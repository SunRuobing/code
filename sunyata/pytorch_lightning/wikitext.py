import os, random, json
from typing import Callable, List
import torch
from torch.utils.data import DataLoader
from datasets import load_dataset
from tokenizers import ByteLevelBPETokenizer

import pytorch_lightning as pl

from sunyata.pytorch.chinese_remainder_theorem import ChineseRemainderTheorem


class WikiTextDataModule(pl.LightningDataModule):
    def __init__(self, subset: str, data_dir:str, batch_size: int, vocab_size: int, seq_len:int, 
                 collate_fn:Callable=None, is_shuffle:bool=False):
        super().__init__()
        assert subset == "2" or subset == "103", 'only support wikitext-2 and wikitext-103'
        self.subset, self.data_dir, self.batch_size, self.vocab_size = subset, data_dir, batch_size, vocab_size
        self.seq_len, self.collate_fn, self.is_shuffle = seq_len, collate_fn, is_shuffle
        self.setup()
    
    def setup(self, stage=None):
        train_tensor_file = os.path.join(self.data_dir, f"wikitext-{self.subset}-vocab-{self.vocab_size}.train.pt")
        validation_tensor_file = os.path.join(self.data_dir, f"wikitext-{self.subset}-vocab-{self.vocab_size}.validation.pt")
        tokenizer_path = os.path.join(self.data_dir, f"wikitext-{self.subset}-vocab-{self.vocab_size}/")

        if os.path.exists(train_tensor_file) and os.path.exists(validation_tensor_file) and os.path.exists(tokenizer_path) and not self.is_shuffle:
            self.train_data = torch.load(train_tensor_file)
            self.validation_data = torch.load(validation_tensor_file)
            self.tokenizer = ByteLevelBPETokenizer.from_file(tokenizer_path + "vocab.json", tokenizer_path + "merges.txt")
        else:
            self.dataset, self.train_data, self.validation_data, self.tokenizer = setup_wikitext(self.subset, self.data_dir, self.vocab_size, self.is_shuffle)

        self.train_data = tensor_strip(self.train_data, self.seq_len)
        self.validation_data = tensor_strip(self.validation_data, self.seq_len)

    def train_dataloader(self):
        return DataLoader(
            self.train_data,
            batch_size=self.batch_size,
            shuffle=True,
            collate_fn=self.collate_fn
        )

    def val_dataloader(self):
        return DataLoader(
            self.validation_data,
            batch_size=self.batch_size,
            shuffle=False,
            collate_fn=self.collate_fn
        )


def split_to_two_parts(batch):
    batch = torch.stack(batch)
    input_row, input_col = torch.div(batch[:, :-1], 100, rounding_mode='trunc'), batch[:, :-1] % 100
    target = batch[:, 1:]
    return (input_row, input_col), target

def shift_one_token(batch):
    batch = torch.stack(batch)
    input = batch[:, :-1]
    target = batch[:, 1:]
    return input, target

def setup_wikitext(subset: str, data_dir: str, vocab_size: int, is_shuffle: bool=False):
    assert subset == "2" or subset == "103", 'only support wikitext-2 and wikitext-103'
    dataset = load_dataset("wikitext", "wikitext-103-v1" if subset=="103" else "wikitext-2-v1")

    train_texts = dataset['train']['text']
    tokenizer = ByteLevelBPETokenizer()
    tokenizer.train_from_iterator(batch_iterator(train_texts), vocab_size=vocab_size,
        min_frequency=2, special_tokens=["<pad>", "<mask>", "<unk>","<eol>"])
    
    tokenizer_path = os.path.join(data_dir, f"wikitext-{subset}-vocab-{vocab_size}/")
    if not os.path.exists(tokenizer_path):
        os.mkdir(tokenizer_path)
    tokenizer.save_model(tokenizer_path)

    if is_shuffle:
        shuffle(tokenizer_path, tokenizer.get_vocab_size(), start_id=4)
        tokenizer = ByteLevelBPETokenizer.from_file(tokenizer_path + "vocab.json", tokenizer_path + "merges.txt")


    train_data = tokenizer.encode_batch([
        line.strip().replace('\n', '') + "<eol>" for line in dataset['train']['text'] if len(line) > 0
        ]) 
    train_ids = [line.ids for line in train_data]
    train_ids = to_tensor(train_ids)
    torch.save(train_ids, os.path.join(data_dir, f"wikitext-{subset}-vocab-{vocab_size}.train.pt"))

    validation_data = tokenizer.encode_batch([
        line.strip().replace('\n', '') + "<eol>" for line in dataset['validation']['text'] if len(line) > 0
        ]) 
    validation_ids = [line.ids for line in validation_data]
    validation_ids = to_tensor(validation_ids)
    torch.save(validation_ids, os.path.join(data_dir, f"wikitext-{subset}-vocab-{vocab_size}.validation.pt"))

    return dataset, train_ids, validation_ids, tokenizer


def shuffle(tokenizer_path, vocab_size: int, start_id: int):
    vocab_file = os.path.join(tokenizer_path, "vocab.json")
    with open(vocab_file, encoding='utf-8') as f:
        vocab = json.load(f)
        
    ids = list(range(start_id, vocab_size))
    random.shuffle(ids)

    shuffled_vocab = [(k, ids[i-start_id]) for i, (k, v) in enumerate(vocab.items()) if v >= start_id]
    shuffled_vocab = dict(shuffled_vocab)

    special_tokens = dict([(k, v) for k, v in vocab.items() if v < start_id])
    special_tokens.update(shuffled_vocab)
    shuffled_vocab = special_tokens

    with open(vocab_file, mode='w', encoding='utf-8') as f:
        json.dump(shuffled_vocab, f, ensure_ascii=False)



def batch_iterator(text: List[str], batch_size: int = 1000):
    for i in range(0, len(text), batch_size):
        yield [line.strip().replace('\n', '') for line in text[i: i + batch_size]]


def to_tensor(lists_ids: List[List[int]]) -> torch.Tensor:
    return torch.tensor([id for list_ids in lists_ids for id in list_ids], dtype=torch.long)

def tensor_strip(t: torch.Tensor, seq_len: int) -> torch.Tensor:
    return t[:len(t) // seq_len * seq_len].view(-1, seq_len)