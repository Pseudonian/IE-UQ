"""THe inference script specifically for extraction on the NERRE dataset. 
Basically the same as few_shot_inference but loads the json rather than the json lines. 
"""

from langchain.prompts import PromptTemplate, FewShotPromptTemplate
import random

from ie_uq.data_load import DataLoad
from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    AutoConfig,
    pipeline,
)
from ie_uq.config_utils import ConfigLoader
from ie_uq.data_preprocess import DataPreprocessOai
from ie_uq.data_load import DataLoad
from ie_uq.extraction_utils import get_text_between_curly_braces
from typing import Optional, Union
import os
import torch
import time
import json
from tqdm import tqdm
import requests
from doping.step2_train_predict import decode_entities_from_llm_completion
import logging
from datasets import Dataset
from transformers.pipelines.pt_utils import KeyDataset


# Function to split data into batches
def batchify(data, batch_size):
    for i in range(0, len(data), batch_size):
        yield data[i : i + batch_size]


def main(
    model_id: str = "meta-llama/Llama-3.2-1B-Instruct",
    dataset_path: str = "https://raw.githubusercontent.com/tlebryk/IE-UQ/refs/heads/develop/data/cleaned_dataset.jsonl",
    inference_dataset_path: str = "https://raw.githubusercontent.com/tlebryk/NERRE/refs/heads/main/doping/data/test.json",
    mode: str = "synth_span",
    output_dir: str = None,
    bnb_dict: Optional[Union[str, dict]] = None,
    # peft_dict: Optional[Union[str, dict]] = None,
    # sft_dict: Optional[Union[str, dict]] = None,
    model_dict: Optional[Union[str, dict]] = None,
    generation_dict: Optional[Union[str, dict]] = None,
    quick_mode: bool = False,
    n_samples: int = 2,
) -> None:
    if not output_dir:
        # use current datetime
        output_dir = f"outputs/{time.strftime('%Y-%m-%d_%H-%M-%S')}"
    os.makedirs(output_dir, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    # BitsAndBytesConfig
    # TODO: add quantization to inference?
    bnb_config = ConfigLoader.load_bnb(bnb_dict)
    # peft_config = ConfigLoader.load_peft(peft_dict)
    # sft_config = ConfigLoader.load_sft(sft_dict, output_dir=output_dir, device=device)
    model_dict = ConfigLoader.load_model_dict(
        model_dict, device=device, bnb_config=bnb_config
    )

    example_dataset = DataLoad.load(dataset_path, split="train")
    example_dataset = example_dataset.map(
        lambda x: {
            "prompt": x["prompt"].replace("{", "{{").replace("}", "}}"),
            "completion": x["completion"].replace("{", "{{").replace("}", "}}"),
        },
    )
    # Define your example template with custom role names
    example_template = """ user {prompt} \n assistant {completion}"""

    # Create a PromptTemplate for the examples
    example_prompt = PromptTemplate(
        input_variables=["prompt", "completion"],
        template=example_template,
    )

    formater = getattr(DataPreprocessOai, mode, lambda x: x)
    system_prompt = getattr(DataPreprocessOai, mode + "_system_prompt", None)

    examples_list = example_dataset.to_pandas().to_dict(orient="records")

    # Sample function that processes sentence_text and returns llm_completion
    def example_llm_function(sentence_text):
        # Replace this function with the actual logic or computation
        return ' {\n "basemats": {\n  "b0": "ZnO"\n },\n "dopants": {\n  "d0": "Al",\n  "d1": "Ga",\n  "d2": "In"\n },\n "dopants2basemats": {\n  "d0": [\n   "b0"\n  ],\n  "d1": [\n   "b0"\n  ],\n  "d2": [\n   "b0"\n  ]\n }\n}'

    def add_few_shot_prompt(
        examples_list=examples_list,
        n_samples=n_samples,
        system_prompt=system_prompt,
    ):
        examples = random.sample(examples_list, n_samples)
        # Create the FewShotPromptTemplate without additional input variables
        few_shot_prompt = FewShotPromptTemplate(
            examples=examples,
            example_prompt=example_prompt,
            prefix="",
            suffix="",
            input_variables=[],  # No additional variables since we don't have a suffix with variables
        )

        # Format the prompt
        final_few_shot = few_shot_prompt.format()
        sys_prompt = (
            f"{system_prompt}"
            " Here are some examples:\n"
            f"{final_few_shot}\n"
            " Now your turn."
        )
        return sys_prompt

    # URL of the JSON data
    # url = 'https://raw.githubusercontent.com/tlebryk/NERRE/refs/heads/main/doping/data/test.json'

    # Fetch JSON data from the URL
    # TODO: refactor to accept local paths too.

    model = AutoModelForCausalLM.from_pretrained(model_id, **model_dict)
    model = model.eval()
    model_config = model.config
    tokenizer_id = model.base_model.config.name_or_path
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_id)
    # reset model to use default chat template
    # tokenizer.chat_template = None
    # model, tokenizer = setup_chat_format(model, tokenizer)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.pad_token_id = tokenizer.eos_token_id
    generation_config = ConfigLoader.load_generation(generation_dict, model_config)
    pipe = pipeline(
        "text-generation",
        model=model,
        tokenizer=tokenizer,
        generation_config=generation_config,
    )

    response = requests.get(inference_dataset_path)
    data = response.json()
    if quick_mode:
        data = data[:1]
    logging.info("training dataset sample:", data[0])
    batch_size = 128
    # all_doping_sentences = []
    # for entry in data:
    #     for dopant_sentence in entry.get("doping_sentences", []):
    #         all_doping_sentences.append((dopant_sentence, entry))

    with torch.no_grad():
        # Iterate over each dictionary in the list
        for entry in tqdm(data):
            # Iterate over each doping_sentence in the nested list
            doping_sentences = entry.get("doping_sentences", [])
            for dopant_sentence in tqdm(doping_sentences):
                # Prepare the prompt for the single sentence
                sentence_text = dopant_sentence.get("sentence_text", "")
                s_prompt = add_few_shot_prompt(examples_list, n_samples)
                messages = formater(
                    {"prompt": sentence_text, "completion": ""},
                    system_prompt=s_prompt,
                )
                prompt = pipe.tokenizer.apply_chat_template(
                    messages["messages"][:-1],
                    tokenize=False,
                    add_generation_prompt=True,
                )

                # Run inference for the single sentence
                generation = pipe(
                    prompt,
                    return_full_text=False,
                    generation_config=generation_config,
                    pad_token_id=tokenizer.eos_token_id,
                )[0]
                dopant_sentence["raw_output"] = generation
                dopant_sentence["full_prompt"] = prompt
                # Process the generated output
                llm_completion = get_text_between_curly_braces(
                    generation["generated_text"]
                )
                dopant_sentence["llm_completion"] = llm_completion
                ents = decode_entities_from_llm_completion(
                    dopant_sentence["llm_completion"], fmt="json"
                )
                dopant_sentence["entity_graph_raw"] = ents
            logging.info(f"{dopant_sentence=}")
    # Save the updated JSON data to a new file
    output_path = os.path.join(output_dir, "fewshot2output.json")
    with open(output_path, "w") as outfile:
        json.dump(data, outfile, indent=2)
