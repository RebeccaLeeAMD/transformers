#!/usr/bin/env python
# coding=utf-8
# Copyright 2018 Google AI, Google Brain and Carnegie Mellon University Authors and the HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Conditional text generation with the auto-regressive models of the library (GPT/GPT-2/CTRL/Transformer-XL/XLNet)

Example usage:
    python bench_generation.py --model_type llama --model_name_or_path meta-llama/Llama-2-13b-chat-hf --torch_dtype float16 --seed 42
"""


import argparse
import inspect
import logging
from typing import Tuple
import typing

import torch
from accelerate import PartialState
from accelerate.utils import set_seed

from transformers import (
    AutoTokenizer,
    BloomForCausalLM,
    BloomTokenizerFast,
    CTRLLMHeadModel,
    CTRLTokenizer,
    GenerationMixin,
    GPT2LMHeadModel,
    GPT2Tokenizer,
    GPTJForCausalLM,
    LlamaForCausalLM,
    LlamaTokenizer,
    OpenAIGPTLMHeadModel,
    OpenAIGPTTokenizer,
    OPTForCausalLM,
    TransfoXLLMHeadModel,
    TransfoXLTokenizer,
    XLMTokenizer,
    XLMWithLMHeadModel,
    XLNetLMHeadModel,
    XLNetTokenizer,
)
from transformers.modeling_outputs import CausalLMOutputWithPast

import os
import sys
import time
import json
from tqdm import tqdm
import statistics


# Set up logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
# Logging file handler
fh = logging.FileHandler(filename='bench_generation.log', mode='w')
fh.setLevel(logging.DEBUG)
# Logging console handler
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.DEBUG)
# Add formatter to handlers
formatter = logging.Formatter(
    fmt="%(asctime)s.%(msecs)03d - %(levelname)s - %(name)s - %(message)s",
    datefmt="%m/%d/%Y %H:%M:%S",
)
fh.setFormatter(formatter)
ch.setFormatter(formatter)
# Add handlers to logger
logger.addHandler(ch)
logger.addHandler(fh)


MAX_LENGTH = int(10000)  # Hardcoded max length to avoid infinite loop

MODEL_CLASSES = {
    "gpt2": (GPT2LMHeadModel, GPT2Tokenizer),
    "ctrl": (CTRLLMHeadModel, CTRLTokenizer),
    "openai-gpt": (OpenAIGPTLMHeadModel, OpenAIGPTTokenizer),
    "xlnet": (XLNetLMHeadModel, XLNetTokenizer),
    "transfo-xl": (TransfoXLLMHeadModel, TransfoXLTokenizer),
    "xlm": (XLMWithLMHeadModel, XLMTokenizer),
    "gptj": (GPTJForCausalLM, AutoTokenizer),
    "bloom": (BloomForCausalLM, BloomTokenizerFast),
    "llama": (LlamaForCausalLM, LlamaTokenizer),
    "opt": (OPTForCausalLM, GPT2Tokenizer),
}

# Padding text to help Transformer-XL and XLNet with short prompts as proposed by Aman Rusia
# in https://github.com/rusiaaman/XLNet-gen#methodology
# and https://medium.com/@amanrusia/xlnet-speaks-comparison-to-gpt-2-ea1a4e9ba39e
PREFIX = """In 1991, the remains of Russian Tsar Nicholas II and his family
(except for Alexei and Maria) are discovered.
The voice of Nicholas's young son, Tsarevich Alexei Nikolaevich, narrates the
remainder of the story. 1883 Western Siberia,
a young Grigori Rasputin is asked by his father and a group of men to perform magic.
Rasputin has a vision and denounces one of the men as a horse thief. Although his
father initially slaps him for making such an accusation, Rasputin watches as the
man is chased outside and beaten. Twenty years later, Rasputin sees a vision of
the Virgin Mary, prompting him to become a priest. Rasputin quickly becomes famous,
with people, even a bishop, begging for his blessing. <eod> </s> <eos>"""


#
# Functions to prepare models' input
#


def prepare_ctrl_input(args, _, tokenizer, prompt_text):
    if args.temperature > 0.7:
        logger.info("CTRL typically works better with lower temperatures (and lower top_k).")

    encoded_prompt = tokenizer.encode(prompt_text, add_special_tokens=False)
    if not any(encoded_prompt[0] == x for x in tokenizer.control_codes.values()):
        logger.info("WARNING! You are not starting your generation from a control code so you won't get good results")
    return prompt_text


def prepare_xlm_input(args, model, tokenizer, prompt_text):
    # kwargs = {"language": None, "mask_token_id": None}

    # Set the language
    use_lang_emb = hasattr(model.config, "use_lang_emb") and model.config.use_lang_emb
    if hasattr(model.config, "lang2id") and use_lang_emb:
        available_languages = model.config.lang2id.keys()
        if args.xlm_language in available_languages:
            language = args.xlm_language
        else:
            language = None
            while language not in available_languages:
                language = input("Using XLM. Select language in " + str(list(available_languages)) + " >>> ")

        model.config.lang_id = model.config.lang2id[language]
        # kwargs["language"] = tokenizer.lang2id[language]

    # TODO fix mask_token_id setup when configurations will be synchronized between models and tokenizers
    # XLM masked-language modeling (MLM) models need masked token
    # is_xlm_mlm = "mlm" in args.model_name_or_path
    # if is_xlm_mlm:
    #     kwargs["mask_token_id"] = tokenizer.mask_token_id

    return prompt_text


def prepare_xlnet_input(args, _, tokenizer, prompt_text):
    prefix = args.prefix if args.prefix else args.padding_text if args.padding_text else PREFIX
    prompt_text = prefix + prompt_text
    return prompt_text


def prepare_transfoxl_input(args, _, tokenizer, prompt_text):
    prefix = args.prefix if args.prefix else args.padding_text if args.padding_text else PREFIX
    prompt_text = prefix + prompt_text
    return prompt_text


PREPROCESSING_FUNCTIONS = {
    "ctrl": prepare_ctrl_input,
    "xlm": prepare_xlm_input,
    "xlnet": prepare_xlnet_input,
    "transfo-xl": prepare_transfoxl_input,
}


def adjust_length_to_model(length, max_sequence_length):
    if length < 0 and max_sequence_length > 0:
        length = max_sequence_length
    elif 0 < max_sequence_length < length:
        length = max_sequence_length  # No generation bigger than model size
    elif length < 0:
        length = MAX_LENGTH  # avoid infinite loop
    return length


def sparse_model_config(model_config):
    embedding_size = None
    if hasattr(model_config, "hidden_size"):
        embedding_size = model_config.hidden_size
    elif hasattr(model_config, "n_embed"):
        embedding_size = model_config.n_embed
    elif hasattr(model_config, "n_embd"):
        embedding_size = model_config.n_embd

    num_head = None
    if hasattr(model_config, "num_attention_heads"):
        num_head = model_config.num_attention_heads
    elif hasattr(model_config, "n_head"):
        num_head = model_config.n_head

    if embedding_size is None or num_head is None or num_head == 0:
        raise ValueError("Check the model config")

    num_embedding_size_per_head = int(embedding_size / num_head)
    if hasattr(model_config, "n_layer"):
        num_layer = model_config.n_layer
    elif hasattr(model_config, "num_hidden_layers"):
        num_layer = model_config.num_hidden_layers
    else:
        raise ValueError("Number of hidden layers couldn't be determined from the model config")

    return num_layer, num_head, num_embedding_size_per_head


def generate_past_key_values(model, batch_size, seq_len):
    num_block_layers, num_attention_heads, num_embedding_size_per_head = sparse_model_config(model.config)
    if model.config.model_type == "bloom":
        past_key_values = tuple(
            (
                torch.empty(int(num_attention_heads * batch_size), num_embedding_size_per_head, seq_len)
                .to(model.dtype)
                .to(model.device),
                torch.empty(int(num_attention_heads * batch_size), seq_len, num_embedding_size_per_head)
                .to(model.dtype)
                .to(model.device),
            )
            for _ in range(num_block_layers)
        )
    else:
        past_key_values = tuple(
            (
                torch.empty(batch_size, num_attention_heads, seq_len, num_embedding_size_per_head)
                .to(model.dtype)
                .to(model.device),
                torch.empty(batch_size, num_attention_heads, seq_len, num_embedding_size_per_head)
                .to(model.dtype)
                .to(model.device),
            )
            for _ in range(num_block_layers)
        )
    return past_key_values


def prepare_jit_inputs(inputs, model, tokenizer):
    batch_size = len(inputs)
    dummy_input = tokenizer.batch_encode_plus(inputs, return_tensors="pt")
    dummy_input = dummy_input.to(model.device)
    if model.config.use_cache:
        dummy_input["past_key_values"] = generate_past_key_values(model, batch_size, 1)
    dummy_input["attention_mask"] = torch.cat(
        [
            torch.zeros(dummy_input["attention_mask"].shape[0], 1)
            .to(dummy_input["attention_mask"].dtype)
            .to(model.device),
            dummy_input["attention_mask"],
        ],
        -1,
    )
    return dummy_input


class _ModelFallbackWrapper(GenerationMixin):
    __slots__ = ("_optimized", "_default")

    def __init__(self, optimized, default):
        self._optimized = optimized
        self._default = default

    def __call__(self, *args, **kwargs):
        if kwargs["past_key_values"] is None and self._default.config.use_cache:
            kwargs["past_key_values"] = generate_past_key_values(self._default, kwargs["input_ids"].shape[0], 0)
        kwargs.pop("position_ids", None)
        for k in list(kwargs.keys()):
            if kwargs[k] is None or isinstance(kwargs[k], bool):
                kwargs.pop(k)
        outputs = self._optimized(**kwargs)
        lm_logits = outputs[0]
        past_key_values = outputs[1]
        fixed_output = CausalLMOutputWithPast(
            loss=None,
            logits=lm_logits,
            past_key_values=past_key_values,
            hidden_states=None,
            attentions=None,
        )
        return fixed_output

    def __getattr__(self, item):
        return getattr(self._default, item)

    def prepare_inputs_for_generation(
        self, input_ids, past_key_values=None, inputs_embeds=None, use_cache=None, **kwargs
    ):
        return self._default.prepare_inputs_for_generation(
            input_ids, past_key_values=past_key_values, inputs_embeds=inputs_embeds, use_cache=use_cache, **kwargs
        )

    def _reorder_cache(
        self, past_key_values: Tuple[Tuple[torch.Tensor]], beam_idx: torch.Tensor
    ) -> Tuple[Tuple[torch.Tensor]]:
        """
        This function is used to re-order the `past_key_values` cache if [`~PretrainedModel.beam_search`] or
        [`~PretrainedModel.beam_sample`] is called. This is required to match `past_key_values` with the correct
        beam_idx at every generation step.
        """
        return self._default._reorder_cache(past_key_values, beam_idx)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model_type",
        default=None,
        type=str,
        required=True,
        help="Model type selected in the list: " + ", ".join(MODEL_CLASSES.keys()),
    )
    parser.add_argument(
        "--model_name_or_path",
        default=None,
        type=str,
        required=True,
        help="Path to pre-trained model or shortcut name selected in the list: " + ", ".join(MODEL_CLASSES.keys()),
    )

    # parser.add_argument("--prompt", type=str, default="Describe the life and reign of King Charles II.")
    parser.add_argument("--prompt", type=str, default="Who invented the game of chess?")
    parser.add_argument("--length", type=int, default=4096)
    parser.add_argument("--stop_token", type=str, default=None, help="Token at which text generation is stopped")

    parser.add_argument(
        "--temperature",
        type=float,
        default=1.0,
        help="temperature of 1.0 has no effect, lower tend toward greedy sampling",
    )
    parser.add_argument(
        "--repetition_penalty", type=float, default=1.0, help="primarily useful for CTRL model; in that case, use 1.2"
    )
    parser.add_argument("--k", type=int, default=0)
    parser.add_argument("--p", type=float, default=0.9)

    parser.add_argument("--prefix", type=str, default="", help="Text added prior to input.")
    parser.add_argument("--padding_text", type=str, default="", help="Deprecated, the use of `--prefix` is preferred.")
    parser.add_argument("--xlm_language", type=str, default="", help="Optional language when used with the XLM model.")

    parser.add_argument("--seed", type=int, default=42, help="random seed for initialization")
    parser.add_argument(
        "--use_cpu",
        action="store_true",
        help="Whether or not to use cpu. If set to False, " "we will use gpu/npu or mps device if available",
    )
    parser.add_argument("--num_return_sequences", type=int, default=1, help="The number of samples to generate.")
    parser.add_argument(
        "--fp16",
        action="store_true",
        help="Whether to use 16-bit (mixed) precision (through NVIDIA apex) instead of 32-bit",
    )
    parser.add_argument("--jit", action="store_true", help="Whether or not to use jit trace to accelerate inference")
    # Added args
    parser.add_argument("--torch_dtype", type=str, default="float32", help="PyTorch datatype used to load model.")
    # parser.add_argument("--use_cache", type=bool, default=True, help="Specifies whether to use KV caching")
    # parser.add_argument(
    #     "--use_cache",
    #     default=True,
    #     action=argparse.BooleanOptionalAction,
    #     help="Toggle so that kv cache is not used",
    # )
    parser.add_argument("--device_map", type=str, default=None, help="Hugging Face Accelerate device_map configuration")
    parser.add_argument("--use_cache", type=str, default="True", help="Toggle kv caching")
    parser.add_argument("--cache_dir", type=str, default="/data", help="Directory for Hugging Face model and dataset cache")
    parser.add_argument("--n_warmup_runs", type=int, default=1, help="Number of warmup runs")
    parser.add_argument("--n_runs", type=int, default=1, help="Number of runs")
    parser.add_argument(
        "--output_sequences",
        action="store_true",
        help="Whether to store output token sequences",
        )
    parser.add_argument(
        "--same_seed",
        action="store_true",
        help="Whether to use the same random seed for each inference",
        )
    parser.add_argument("--records_file", type=str, default="records.json", help="Filename for generated output data records")
    parser.add_argument("--metrics_file", type=str, default="metrics.json", help="Filename for generated output data metrics")
    parser.add_argument("--max_new_tokens", type=int, default=None, help="Maimum new tokens generated")
    parser.add_argument("--min_new_tokens", type=int, default=None, help="Minimum new tokens generated")
    parser.add_argument("--preset_prompt", type=str, default=None, help="Preset prompt")
    parser.add_argument(
        "--print_records",
        action="store_true",
        help="Whether to print output records",
        )
    #
    args = parser.parse_args()

    # Set torch_dtype
    if args.torch_dtype == "float16":
        args.torch_dtype = torch.float16
    elif args.torch_dtype == "auto":
        pass
    else:
        args.torch_dtype = torch.float32

    # Set use_cache
    if args.use_cache == "False":
        args.use_cache = False
    else:
        args.use_cache = True
    
    # Format device_map
    if args.device_map and args.device_map.isdigit():
        args.device_map = int(args.device_map)
    
    # Preset prompt
    if args.preset_prompt:
        prompt_file = os.path.abspath(os.path.join(os.path.dirname(__file__), f'prompts/{args.preset_prompt}.txt'))
        if not os.path.isfile(prompt_file):
            raise ValueError(f"Prompt file does not exist: {prompt_file}")
        
        with open(prompt_file, 'r') as file:
            args.prompt = file.read()


    # Initialize the distributed state.
    distributed_state = PartialState(cpu=args.use_cpu)
    if args.device_map is None:
        my_device = distributed_state.device
    elif isinstance(args.device_map, int):
        my_device = f"cuda:{args.device_map}"
    else:
        my_device = None

    logger.warning(f"device: {distributed_state.device}, 16-bits inference: {args.fp16}")

    if args.seed is not None:
        set_seed(args.seed)

    # Initialize the model and tokenizer
    try:
        args.model_type = args.model_type.lower()
        model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    except KeyError:
        raise KeyError("the model {} you specified is not supported. You are welcome to add it and open a PR :)")

    tokenizer = tokenizer_class.from_pretrained(args.model_name_or_path)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    
    logger.debug(f"Model - Load - start")
    # model = model_class.from_pretrained(args.model_name_or_path, torch_dtype=args.torch_dtype, use_cache=args.use_cache, cache_dir=args.cache_dir, device_map=args.device_map)
    model = model_class.from_pretrained(args.model_name_or_path, torch_dtype=args.torch_dtype, cache_dir=args.cache_dir, device_map=args.device_map)

    # Set the model to the right device
    if my_device:
        model.to(my_device)

    if args.fp16:
        model.half()
    logger.debug(f"Model - Load - end")
    
    max_seq_length = getattr(model.config, "max_position_embeddings", 0)
    args.length = adjust_length_to_model(args.length, max_sequence_length=max_seq_length)
    logger.info(args)

    if args.jit:
        jit_input_texts = ["enable jit"]
        jit_inputs = prepare_jit_inputs(jit_input_texts, model, tokenizer)
        torch._C._jit_set_texpr_fuser_enabled(False)
        model.config.return_dict = False
        if hasattr(model, "forward"):
            sig = inspect.signature(model.forward)
        else:
            sig = inspect.signature(model.__call__)
        jit_inputs = tuple(jit_inputs[key] for key in sig.parameters if jit_inputs.get(key, None) is not None)
        traced_model = torch.jit.trace(model, jit_inputs, strict=False)
        traced_model = torch.jit.freeze(traced_model.eval())
        traced_model(*jit_inputs)
        traced_model(*jit_inputs)

        model = _ModelFallbackWrapper(traced_model, model)

    prompt_text = args.prompt if args.prompt else input("Model prompt >>> ")

    requires_preprocessing = args.model_type in PREPROCESSING_FUNCTIONS.keys()

    # Benchmark
    records = []
    logger.debug("Benchmark - start")
    for i in tqdm(range(args.n_warmup_runs + args.n_runs)):
        logger.debug(f"Benchmark - Iteration[{i}] - start")
        start_time = time.time()

        # Different models need different input formatting and/or extra arguments
        # Tokenize input prompt       
        logger.debug(f"Benchmark - Iteration[{i}] - Tokenize - start")
        if requires_preprocessing:
            prepare_input = PREPROCESSING_FUNCTIONS.get(args.model_type)
            preprocessed_prompt_text = prepare_input(args, model, tokenizer, prompt_text)

            if model.__class__.__name__ in ["TransfoXLLMHeadModel"]:
                tokenizer_kwargs = {"add_space_before_punct_symbol": True}
            else:
                tokenizer_kwargs = {}

            encoded_prompt = tokenizer.encode(
                preprocessed_prompt_text, add_special_tokens=False, return_tensors="pt", **tokenizer_kwargs
            )
        else:
            prefix = args.prefix if args.prefix else args.padding_text
            encoded_prompt = tokenizer.encode(prefix + prompt_text, add_special_tokens=False, return_tensors="pt")
        encoded_prompt = encoded_prompt.to(my_device)
        logger.debug(f"Benchmark - Iteration[{i}] - Tokenize - end")

        if encoded_prompt.size()[-1] == 0:
            input_ids = None
        else:
            input_ids = encoded_prompt
        
        max_length = args.length + len(encoded_prompt[0])
        
        # Run inference
        if args.same_seed:
            set_seed(args.seed)
        
        logger.debug(f"Benchmark - Iteration[{i}] - Generate - start")
        output_sequences = model.generate(
            use_cache=args.use_cache,
            input_ids=input_ids,
            max_length=max_length,
            max_new_tokens=args.max_new_tokens,
            min_new_tokens=args.min_new_tokens,
            temperature=args.temperature,
            top_k=args.k,
            top_p=args.p,
            repetition_penalty=args.repetition_penalty,
            do_sample=True,
            num_return_sequences=args.num_return_sequences,
        )
        logger.debug(f"Benchmark - Iteration[{i}] - Generate - end")
        
        logger.debug(f"Benchmark - Iteration[{i}] - De-tokenize - start")
        # output_sequences = output_sequences.to('cpu')
        output_sequences = output_sequences.to('cpu').detach().numpy()

        # Detokenize to output text
        # Remove the batch dimension when returning multiple sequences
        if len(output_sequences.shape) > 2:
            output_sequences.squeeze_()

        generated_sequences = []

        for generated_sequence_idx, generated_sequence in enumerate(output_sequences):
            # print(f"=== GENERATED SEQUENCE {generated_sequence_idx + 1} ===")
            generated_sequence = generated_sequence.tolist()

            # Decode text
            text = tokenizer.decode(generated_sequence, clean_up_tokenization_spaces=True)

            # Remove all text after the stop token
            text = text[: text.find(args.stop_token) if args.stop_token else None]

            # Add the prompt at the beginning of the sequence. Remove the excess text that was used for pre-processing
            total_sequence = (
                prompt_text + text[len(tokenizer.decode(encoded_prompt[0], clean_up_tokenization_spaces=True)) :]
            )

            generated_sequences.append(total_sequence)
            # print(total_sequence)
        logger.debug(f"Benchmark - Iteration[{i}] - De-tokenize - end")

        end_time = time.time()
        runtime = end_time - start_time

        logger.debug(f"Benchmark - Iteration[{i}] - end")

        input_sequences = input_ids.to('cpu').detach().numpy().tolist()
        
        # Populate records
        record = {
            "latency": runtime,
            "warmup": True if i < args.n_warmup_runs else False,
            "input_lengths": [len(seq) for seq in input_sequences],
            "output_lengths": [len(seq) for seq in output_sequences],
            "total_tokens": sum([len(output_sequence) for output_sequence in output_sequences]),
            "batch_size": len(output_sequences),
            "max_length": max_length,
            "max_new_tokens": args.max_new_tokens,
            "min_new_tokens": args.min_new_tokens,            
        }
        record["tokens_per_second"] = record["total_tokens"] / record["latency"]
        if args.output_sequences:
            record["input_sequences"] = input_ids.to('cpu').detach().numpy().tolist()
            record["output_sequences"] = output_sequences.tolist()
            record["outputs"] = generated_sequences            
        
        records.append(record)

    # Populate metrics
    metrics = {
        "median_warmup_tokens_per_second": statistics.median( [rec['tokens_per_second'] for rec in records if rec["warmup"]==True] ),
        "median_tokens_per_second": statistics.median( [rec['tokens_per_second'] for rec in records if rec["warmup"]==False] ),
    }

    print(f"Metrics for bench_generation.py: {metrics}")

    if args.print_records:
        print()
        print("---------------------------------------------------------------")
        logger.info("Print records - start")
        print("records")
        print("----")
        print(records)
        print("----")
        logger.info("Print records - end")
        print("---------------------------------------------------------------")


    logger.debug("Benchmark - end")


    # Save output data to file
    with open(args.records_file, "w") as fp:
        json.dump(records, fp)
        logger.info(f"Output records written to {args.records_file}")
    
    with open(args.metrics_file, "w") as fp:
        json.dump(metrics, fp)
        logger.info(f"Output metrics written to {args.metrics_file}")

    return records


if __name__ == "__main__":
    logger.debug("Main - start")
    main()
    logger.debug("Main - end")