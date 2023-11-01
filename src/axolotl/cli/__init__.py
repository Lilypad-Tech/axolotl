"""Prepare and train a model on a dataset. Can also infer from a model or merge lora"""

import importlib
import logging
import os
import random
import sys
import io
import base64
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import json
import time
import requests
from datetime import datetime

import torch
import yaml
from contextlib import contextmanager

# add src to the pythonpath so we don't need to pip install this
from accelerate.commands.config import config_args
from art import text2art
from huggingface_hub import HfApi
from huggingface_hub.utils import LocalTokenNotFoundError
from transformers import GenerationConfig, TextStreamer

from axolotl.common.cli import TrainerCliArgs, load_model_and_tokenizer
from axolotl.logging_config import configure_logging
from axolotl.train import TrainDatasetMeta
from axolotl.utils.config import normalize_config, validate_config
from axolotl.utils.data import prepare_dataset
from axolotl.utils.dict import DictDefault
from axolotl.utils.distributed import is_main_process
from axolotl.utils.models import load_tokenizer
from axolotl.utils.tokenization import check_dataset_labels
from axolotl.utils.wandb_ import setup_wandb_env_vars

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
src_dir = os.path.join(project_root, "src")
sys.path.insert(0, src_dir)

configure_logging()
LOG = logging.getLogger("axolotl.scripts")

os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"


def print_axolotl_text_art(suffix=None):
    font = "nancyj"
    ascii_text = "  axolotl"
    if suffix:
        ascii_text += f"  x  {suffix}"
    ascii_art = text2art(" axolotl", font=font)

    if is_main_process():
        print(ascii_art)


def get_multi_line_input() -> Optional[str]:
    #print("Give me an instruction (Ctrl + D to submit): ")
    instruction = ""
    for line in sys.stdin:
        instruction += line  # pylint: disable=consider-using-join
    # instruction = pathlib.Path("/proc/self/fd/0").read_text()
    return instruction


def do_merge_lora(
    *,
    cfg: DictDefault,
    cli_args: TrainerCliArgs,
):
    model, tokenizer = load_model_and_tokenizer(cfg=cfg, cli_args=cli_args)
    safe_serialization = cfg.save_safetensors is True

    LOG.info("running merge of LoRA with base model")
    model = model.merge_and_unload()
    model.to(dtype=torch.float16)

    if cfg.local_rank == 0:
        LOG.info(f"saving merged model to: {str(Path(cfg.output_dir) / 'merged')}")
        model.save_pretrained(
            str(Path(cfg.output_dir) / "merged"),
            safe_serialization=safe_serialization,
        )
        tokenizer.save_pretrained(str(Path(cfg.output_dir) / "merged"))

currentOutputChunks = []

@contextmanager
def redirect_stdout_to_function(func, buffer_size=1024, url="", sessionid=""):
    class BufferedBytesStream(io.BytesIO):
        def __init__(self, buffer_size):
            super().__init__()
            self.buffer_size = buffer_size
            self.buffer = bytearray()

        def write(self, b):
            if isinstance(b, str):
                b = b.encode('utf-8')
            self.buffer.extend(b)
            while len(self.buffer) >= self.buffer_size:
                chunk, self.buffer = self.buffer[:self.buffer_size], self.buffer[self.buffer_size:]
                # this is capture_model_output_chunk
                func(url, sessionid, chunk)

    original_stdout = sys.stdout
    sys.stdout = BufferedBytesStream(buffer_size)

    try:
        yield
    finally:
        # Flush remaining bytes in buffer, if any
        if len(sys.stdout.buffer) > 0:
            # this is capture_model_output_chunk
            func(url, sessionid, sys.stdout.buffer)
        sys.stdout = original_stdout

def send_response(url, session_id, action, message):
    json_payload = json.dumps({
        "type": action,
        "session_id": session_id,
        "message": message
    })
    requests.post(url, data=json_payload)

def capture_model_output_chunk(url, session_id, b):
    global currentOutputChunks
    message = b.decode('utf-8')
    currentOutputChunks.append(message)
    send_response(url, session_id, "stream", message)

def do_inference(
    *,
    cfg: DictDefault,
    cli_args: TrainerCliArgs,
):
    global currentOutputChunks
    waitLoops = 0

    # the url of where we ask for new jobs
    # as soon as we have finished the current job, we will ask for another one
    # if this fails - it means there are no jobs so wait 1 second then ask again
    getJobURL = os.environ.get("HELIX_GET_JOB_URL", None)
    respondJobURL = os.environ.get("HELIX_RESPOND_JOB_URL", None)

    if getJobURL is None:
        sys.exit("HELIX_GET_JOB_URL is not set")

    if respondJobURL is None:
        sys.exit("HELIX_RESPOND_JOB_URL is not set")

    model, tokenizer = load_model_and_tokenizer(cfg=cfg, cli_args=cli_args)
    prompter = cli_args.prompter
    default_tokens = {"unk_token": "<unk>", "bos_token": "<s>", "eos_token": "</s>"}

    for token, symbol in default_tokens.items():
        # If the token isn't already specified in the config, add it
        if not (cfg.special_tokens and token in cfg.special_tokens):
            tokenizer.add_special_tokens({token: symbol})

    prompter_module = None
    if prompter:
        prompter_module = getattr(
            importlib.import_module("axolotl.prompters"), prompter
        )

    if cfg.landmark_attention:
        from axolotl.monkeypatch.llama_landmark_attn import set_model_mem_id

        set_model_mem_id(model, tokenizer)
        model.set_mem_cache_args(
            max_seq_len=255, mem_freq=50, top_k=5, max_cache_size=None
        )

    model = model.to(cfg.device)

    session_id = ""
    last_prompt = ""
    
    while True:
        if len(currentOutputChunks) > 0:
            parts = "".join(currentOutputChunks).split("[/INST]")
            parsedResult = parts[1]
            parsedResult = parsedResult.replace("</s>", "")
            print("🟣 Mistral Question --------------------------------------------------\n")
            print(last_prompt)
            print("🟣 Mistral Answer --------------------------------------------------\n")
            print(parsedResult)
            if session_id != "":
                send_response(respondJobURL, task["session_id"], "result", parsedResult)

        currentOutputChunks = []
        currentJobData = ""

        # TODO: we need to include the fine-tuning model here
        response = requests.get(getJobURL)

        if response.status_code != 200:
            time.sleep(0.1)
            waitLoops = waitLoops + 1
            if waitLoops % 10 == 0:
                print("--------------------------------------------------\n")
                current_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                print(f"{current_timestamp} waiting for next job {getJobURL} {response.status_code}")
            continue

        waitLoops = 0
        currentJobData = response.content

        # print out the response content to stdout
        print("🟣 Mistral Job --------------------------------------------------\n")
        print(currentJobData)

        task = json.loads(currentJobData)
        instruction: str = task["prompt"]
        session_id = task["session_id"]
        last_prompt = instruction

        if prompter_module:
            prompt: str = next(
                prompter_module().build_prompt(instruction=instruction.strip("\n"))
            )
        else:
            prompt = instruction.strip()
        batch = tokenizer(prompt, return_tensors="pt", add_special_tokens=True)

        model.eval()
        with torch.no_grad():
            generation_config = GenerationConfig(
                repetition_penalty=1.1,
                max_new_tokens=1024,
                temperature=0.9,
                top_p=0.95,
                top_k=40,
                bos_token_id=tokenizer.bos_token_id,
                eos_token_id=tokenizer.eos_token_id,
                pad_token_id=tokenizer.pad_token_id,
                do_sample=True,
                use_cache=True,
                return_dict_in_generate=True,
                output_attentions=False,
                output_hidden_states=False,
                output_scores=False,
            )
            streamer = TextStreamer(tokenizer)
            with redirect_stdout_to_function(capture_model_output_chunk, buffer_size=5, url=respondJobURL, sessionid=task["session_id"]):
                generated = model.generate(
                    inputs=batch["input_ids"].to(cfg.device),
                    generation_config=generation_config,
                    streamer=streamer,
                )
                time.sleep(0.1)
        


def choose_config(path: Path):
    yaml_files = list(path.glob("*.yml"))

    if not yaml_files:
        raise ValueError(
            "No YAML config files found in the specified directory. Are you using a .yml extension?"
        )

    if len(yaml_files) == 1:
        print(f"Using default YAML file '{yaml_files[0]}'")
        return yaml_files[0]

    print("Choose a YAML file:")
    for idx, file in enumerate(yaml_files):
        print(f"{idx + 1}. {file}")

    chosen_file = None
    while chosen_file is None:
        try:
            choice = int(input("Enter the number of your choice: "))
            if 1 <= choice <= len(yaml_files):
                chosen_file = yaml_files[choice - 1]
            else:
                print("Invalid choice. Please choose a number from the list.")
        except ValueError:
            print("Invalid input. Please enter a number.")

    return chosen_file


def check_not_in(list1: List[str], list2: Union[Dict[str, Any], List[str]]) -> bool:
    return not any(el in list2 for el in list1)


def load_cfg(config: Path = Path("examples/"), **kwargs):
    if Path(config).is_dir():
        config = choose_config(config)

    # load the config from the yaml file
    with open(config, encoding="utf-8") as file:
        cfg: DictDefault = DictDefault(yaml.safe_load(file))
    cfg.axolotl_config_path = config
    # if there are any options passed in the cli, if it is something that seems valid from the yaml,
    # then overwrite the value
    cfg_keys = cfg.keys()
    for k, _ in kwargs.items():
        # if not strict, allow writing to cfg even if it's not in the yml already
        if k in cfg_keys or not cfg.strict:
            # handle booleans
            if isinstance(cfg[k], bool):
                cfg[k] = bool(kwargs[k])
            else:
                cfg[k] = kwargs[k]

    validate_config(cfg)

    normalize_config(cfg)

    setup_wandb_env_vars(cfg)
    return cfg


def load_datasets(
    *,
    cfg: DictDefault,
    cli_args: TrainerCliArgs,
) -> TrainDatasetMeta:
    tokenizer = load_tokenizer(cfg)

    train_dataset, eval_dataset, total_num_steps = prepare_dataset(cfg, tokenizer)

    if cli_args.debug or cfg.debug:
        LOG.info("check_dataset_labels...")
        check_dataset_labels(
            train_dataset.select(
                [
                    random.randrange(0, len(train_dataset) - 1)  # nosec
                    for _ in range(cli_args.debug_num_examples)
                ]
            ),
            tokenizer,
            num_examples=cli_args.debug_num_examples,
            text_only=cli_args.debug_text_only,
        )

    return TrainDatasetMeta(
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        total_num_steps=total_num_steps,
    )


def check_accelerate_default_config():
    if Path(config_args.default_yaml_config_file).exists():
        LOG.warning(
            f"accelerate config file found at {config_args.default_yaml_config_file}. This can lead to unexpected errors"
        )


def check_user_token():
    # Verify if token is valid
    api = HfApi()
    try:
        user_info = api.whoami()
        return bool(user_info)
    except LocalTokenNotFoundError:
        LOG.warning(
            "Error verifying HuggingFace token. Remember to log in using `huggingface-cli login` and get your access token from https://huggingface.co/settings/tokens if you want to use gated models or datasets."
        )
        return False
