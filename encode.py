import logging
import os
import pickle
import sys
from contextlib import nullcontext

import numpy as np
from tqdm import tqdm

import torch

from torch.utils.data import DataLoader
from transformers import AutoTokenizer
from transformers import (
    HfArgumentParser,
)
from dataset import PromptRepsEncodeDataset, PromptRepsEncodeCollator
import json
from tevatron.retriever.arguments import ModelArguments, \
    TevatronTrainingArguments as TrainingArguments
from arguments import PromptRepsDataArguments
from tevatron.retriever.modeling import EncoderOutput
from modeling import PromptRepsLLM
from nltk import word_tokenize
from nltk.corpus import stopwords
import string
stopwords = set(stopwords.words('english') + list(string.punctuation))

logger = logging.getLogger(__name__)


def main():
    parser = HfArgumentParser((ModelArguments, PromptRepsDataArguments, TrainingArguments))
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        model_args, data_args, training_args = parser.parse_json_file(json_file=os.path.abspath(sys.argv[1]))
    else:
        model_args, data_args, training_args = parser.parse_args_into_dataclasses()
        model_args: ModelArguments
        data_args: PromptRepsDataArguments
        training_args: TrainingArguments

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO if training_args.local_rank in [-1, 0] else logging.WARN,
    )

    tokenizer = AutoTokenizer.from_pretrained(
        model_args.tokenizer_name if model_args.tokenizer_name else model_args.model_name_or_path,
        cache_dir=model_args.cache_dir
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token_id = tokenizer.unk_token_id if tokenizer.unk_token_id else tokenizer.eos_token_id
    tokenizer.padding_side = 'right'

    if training_args.bf16:
        torch_dtype = torch.bfloat16
    elif training_args.fp16:
        torch_dtype = torch.float16
    else:
        torch_dtype = torch.float32

    model = PromptRepsLLM.load(
        model_args.model_name_or_path,
        pooling=model_args.pooling,
        normalize=model_args.normalize,
        lora_name_or_path=model_args.lora_name_or_path,
        cache_dir=model_args.cache_dir,
        trust_remote_code=True,
        torch_dtype=torch_dtype
    )

    encode_dataset = PromptRepsEncodeDataset(
        data_args=data_args,
    )

    encode_collator = PromptRepsEncodeCollator(
        data_args=data_args,
        tokenizer=tokenizer,
    )

    encode_loader = DataLoader(
        encode_dataset,
        batch_size=training_args.per_device_eval_batch_size,
        collate_fn=encode_collator,
        shuffle=False,
        drop_last=False,
        num_workers=training_args.dataloader_num_workers,
    )
    encoded = []
    lookup_indices = []
    jsonl_data = []

    vocab_dict = tokenizer.get_vocab()
    vocab_dict = {v: k for k, v in vocab_dict.items()}

    model.eval()

    for (batch_ids, batch, batch_texts) in tqdm(encode_loader):
        lookup_indices.extend(batch_ids)

        with torch.cuda.amp.autocast() if training_args.fp16 else nullcontext():
            with torch.no_grad():
                batch = batch.to(training_args.device)
                if data_args.encode_is_query:
                    model_output: EncoderOutput = model(query=batch)
                    next_token_logits, next_token_reps = model_output.q_reps
                else:
                    model_output: EncoderOutput = model(passage=batch)
                    next_token_logits, next_token_reps = model_output.p_reps

                encoded.append(next_token_reps.cpu().detach().float().numpy())

                for docid, logits, text in zip(batch_ids, next_token_logits, batch_texts):
                    vector = dict()

                    if not data_args.sparse_exact_match:
                        if not data_args.encode_is_query:
                            # no constraint
                            indices = torch.nonzero(logits)
                            values = logits[indices]
                            values = np.rint(values.cpu().detach().float().numpy() * 100).astype(int)
                            tokens = [vocab_dict[i.item()] for i in indices.cpu().detach().float().numpy()]
                        else:
                            # topk constraint
                            top_k_values, top_k_indices = logits.topk(1000, dim=-1)
                            values = np.rint(top_k_values.cpu().detach().float().numpy() * 100).astype(int)
                            tokens = [vocab_dict[i.item()] for i in top_k_indices.cpu().detach().float().numpy()]
                    else:
                        words = [i for i in word_tokenize(text.lower()) if i not in stopwords]
                        token_ids = set()
                        for word in words:
                            token_ids.update(tokenizer.encode(word, add_special_tokens=False))

                        # top tokens in the text
                        token_ids_in_text = torch.tensor(list(token_ids))
                        if len(token_ids_in_text) == 0:
                            top_k_values, top_k_indices = logits.topk(10, dim=-1)
                            values = np.rint(top_k_values.cpu().detach().float().numpy() * 100).astype(int)
                            tokens = [vocab_dict[i.item()] for i in top_k_indices.cpu().detach().float().numpy()]
                        else:
                            top_k = min(len(token_ids_in_text), 128)
                            top_k_values, top_k_indices = logits[token_ids_in_text].topk(top_k, dim=-1)
                            values = np.rint(top_k_values.cpu().detach().float().numpy() * 100).astype(int)
                            tokens = [vocab_dict[i.item()] for i in token_ids_in_text[top_k_indices.cpu().detach().float().numpy()]]

                    # top tokens not in the text
                    # token_ids_out_text = torch.tensor(list(filtered_ids - token_ids))
                    # top_k = min(20, len(token_ids_out_text))
                    # top_k_values, top_k_indices = logits[token_ids_out_text].topk(top_k, dim=-1)
                    # values = np.rint(top_k_values.cpu().detach().float().numpy() * 100).astype(int)
                    # tokens = [vocab_dict[i.item()] for i in token_ids_out_text[top_k_indices.cpu().detach().float().numpy()]]

                    for token, v in zip(tokens, values):
                        vector[token] = int(v)
                    jsonl_data.append(
                        dict(
                            id=docid,
                            content="",
                            vector=vector,
                        )
                    )

    encoded = np.concatenate(encoded)

    os.makedirs(data_args.dense_output_dir, exist_ok=True)
    with open(os.path.join(data_args.dense_output_dir, 'query.pkl' if data_args.encode_is_query else f'corpus_{data_args.dataset_shard_index}.pkl'), 'wb') as f:
        pickle.dump((encoded, lookup_indices), f)

    os.makedirs(data_args.sparse_output_dir, exist_ok=True)
    with open(os.path.join(data_args.sparse_output_dir, 'query.tsv' if data_args.encode_is_query else f'corpus_{data_args.dataset_shard_index}.jsonl'), 'w') as f:
        for data in jsonl_data:
            if data_args.encode_is_query:
                id = data['id']
                vector = data['vector']
                query = " ".join(
                            [" ".join([str(token)] * freq) for token, freq in vector.items()])
                if len(query.strip()) == 0:
                    continue
                f.write(f"{id}\t{query}\n")
            else:
                f.write(json.dumps(data) + "\n")


if __name__ == "__main__":
    main()
