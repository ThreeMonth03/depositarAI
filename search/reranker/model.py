"""Pinned FP32 NVIDIA cross-encoder, fully resident on exactly one GPU."""

import hashlib
from pathlib import Path
import time

from .contract import INFERENCE_BUDGET_S, prompt, rank_scores

MODEL_HASHES = {
    "model.safetensors": "7d60ff24db62fe6a639c4c6f4aeac3a3b32ed20939ab72a0be4b019c2219e5e0",
    "llama_bidirectional_model.py": "f0911d5b1ff55fed248a0f10a9a5dea5af3e61425b0d00617404688a3b4b3196",
    "config.json": "34d45f095f896f950db008c19e2bb4c935fc2866f71212c0cd26022eb1167d7b",
    "tokenizer_config.json": "c201a1b5b69f8692cb614d087de6b5f40f691d06a2033f9ee78dc58e6f391080",
    "tokenizer.json": "79e3e522635f3171300913bb421464a87de6222182a0570b9b2ccba2a964b2b4",
    "special_tokens_map.json": "03d62862d41de30db9e05cb4865de4f36c48ab3032327139fcdfacc5798a4828",
}


def verify_model(directory):
    for name, expected in MODEL_HASHES.items():
        digest = hashlib.sha256()
        with (Path(directory) / name).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError("model identity mismatch")


class Ranker:
    def __init__(self, directory):
        verify_model(directory)  # Includes local custom code; before loading it.
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
            raise RuntimeError("exactly one GPU required; no CPU fallback")
        self.torch = torch
        torch.set_num_threads(8)
        torch.set_num_interop_threads(1)
        self.tokenizer = AutoTokenizer.from_pretrained(
            directory, local_files_only=True, padding_side="left"
        )
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.model = (
            AutoModelForSequenceClassification.from_pretrained(
                directory,
                local_files_only=True,
                trust_remote_code=True,
                torch_dtype=torch.float32,
                attn_implementation="eager",
                use_safetensors=True,
            )
            .eval()
            .to("cuda:0")
        )
        self.model.config.use_cache = False
        self.model.config.pad_token_id = self.tokenizer.pad_token_id
        if any(str(p.device) != "cuda:0" for p in self.model.parameters()):
            raise RuntimeError("model is not fully on GPU")
        # Health becomes available only after CUDA/model warm-up has succeeded.
        self.rank("Where is Taipei?", ["Taipei is in Taiwan."])

    def rank(self, query, passages):
        deadline = time.monotonic() + INFERENCE_BUDGET_S
        scores = []
        for document in passages:
            if time.monotonic() >= deadline:
                raise TimeoutError("ranking budget exceeded")
            batch = self.tokenizer(
                [prompt(query, document)],
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            )
            batch = {key: value.to("cuda:0") for key, value in batch.items()}
            with self.torch.inference_mode():
                value = (
                    self.model(**batch, use_cache=False).logits.view(-1).cpu().tolist()
                )
            if len(value) != 1:
                raise ValueError("invalid logits")
            scores.extend(value)
        if time.monotonic() >= deadline:
            raise TimeoutError("ranking budget exceeded")
        return rank_scores(scores)
