import json
import numpy as np
from pyvi import ViTokenizer
from datasets import Dataset, load_metric, load_dataset
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM, Seq2SeqTrainer, Trainer, TrainingArguments, \
    Seq2SeqTrainingArguments, DataCollatorForSeq2Seq
from tqdm.notebook import tqdm
from torch.utils.data import DataLoader
import torch
import os
import evaluate
from dataclasses import dataclass
from nltk.translate.bleu_score import sentence_bleu
import spacy
from loguru import logger
import nltk

nltk.download("wordnet")

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

nlp = spacy.load('vi_core_news_lg')


@dataclass
class Config:
    model_name: str = 'vit5'
    dataset_name: str = 'vinewsqa'  # viquad, vimmrc, vimmrc2.0, vicoqa
    pretrained_model_name_or_path: str = 'VietAI/vit5-base'
    checkpoint: str = '/kaggle/working/cp'
    task: str = 'ag'  # ag
    tgt_len: int = 256
    src_len: int = 1024
    seed: int = 42
    num_proc: int = 16
    per_device_train_batch_size: int = 4
    per_device_eval_batch_size: int = 16
    num_epochs: int = 10
    lr: float = 1e-4
    warmup_ratio: float = 0.05
    weight_decay: float = 0.015

    def __post_init__(self):
        if self.model_name == 'bartpho':
            self.pretrained_model_name_or_path = 'vinai/bartpho-word-base'


def formatting_func_qg(example):
    input_seq: str = ("### Instruction: \n"
                      f"{example['instruction_qg']}\n\n"
                      f"### Context: \n"
                      f"{ViTokenizer.tokenize(example['context'])}\n\n"
                      f"### Answer: \n"
                      f"{ViTokenizer.tokenize(example['answer'])}\n\n"
                      f"\n\n### Response: \n")

    output_seq: str = ViTokenizer.tokenize(example['question'])

    return {"input_seq": input_seq, 'output_seq': output_seq}


def formatting_func_ag(example):
    input_seq: str = ("### Instruction: \n"
                      f"{example['instruction_ag']}\n\n"
                      f"### Context: \n"
                      f"{ViTokenizer.tokenize(example['context'])}\n\n"
                      f"### Question: \n"
                      f"{ViTokenizer.tokenize(example['question'])}\n\n"
                      f"\n\n### Response: \n")

    output_seq: str = ViTokenizer.tokenize(example['answer'])

    return {"input_seq": input_seq, 'output_seq': output_seq}


def bleu(predict, goal):
    bleu_scores = {1: [], 2: [], 3: [], 4: []}

    for sent1, sent2 in zip(predict, goal):
        sent1_doc = nlp(sent1)
        sent2_doc = nlp(sent2)
        ws = [(1, 0, 0, 0), (0.5, 0.5, 0, 0), (0.33, 0.33, 0.33, 0), (0.25, 0.25, 0.25, 0.25)]
        for n in range(1, 5):
            weights = ws[n - 1]
            sent1_tokens = [token.text for token in sent1_doc]
            sent2_tokens = [token.text for token in sent2_doc]
            bleu_score = sentence_bleu([sent1_tokens], sent2_tokens, weights=weights)
            bleu_scores[n].append(bleu_score)
    result = {}
    for n in range(1, 5):
        avg_bleu_score = (sum(bleu_scores[n]) / len(bleu_scores[n])) * 100
        result["BLEU{}".format(n)] = (sum(bleu_scores[n]) / len(bleu_scores[n])) * 100
    return result


def prepare_data(conf: Config):
    logger.info('-----:----- Preparing dataset -----:-----')
    data = load_dataset(f'shnl/{conf.dataset_name}', use_auth_token=True)
    train_dataset = data['train']
    dev_dataset = data['validation']
    test_dataset = data['test']
    if conf.task == 'qg':
        train_dataset = train_dataset.map(formatting_func_qg, num_proc=conf.num_proc).remove_columns(
            ['instruction_qg', 'instruction_ag', 'context', 'question', 'answer'])
        dev_dataset = dev_dataset.map(formatting_func_qg, num_proc=conf.num_proc).remove_columns(
            ['instruction_qg', 'instruction_ag', 'context', 'question', 'answer'])
        test_dataset = test_dataset.map(formatting_func_qg, num_proc=conf.num_proc).remove_columns(
            ['instruction_qg', 'instruction_ag', 'context', 'question', 'answer'])
    else:
        train_dataset = train_dataset.map(formatting_func_ag, num_proc=conf.num_proc).remove_columns(
            ['instruction_qg', 'instruction_ag', 'context', 'question', 'answer'])
        dev_dataset = dev_dataset.map(formatting_func_ag, num_proc=conf.num_proc).remove_columns(
            ['instruction_qg', 'instruction_ag', 'context', 'question', 'answer'])
        test_dataset = test_dataset.map(formatting_func_ag, num_proc=conf.num_proc).remove_columns(
            ['instruction_qg', 'instruction_ag', 'context', 'question', 'answer'])
    return train_dataset, dev_dataset, test_dataset


def compute_metric(
        conf: Config,
        model: AutoModelForSeq2SeqLM,
        tokenizer: AutoTokenizer,
        tokenized_test: Dataset
):
    data_collator = DataCollatorForSeq2Seq(tokenizer, model=model, return_tensors="pt")
    dataloader = torch.utils.data.DataLoader(tokenized_test, collate_fn=data_collator,
                                             batch_size=conf.per_device_eval_batch_size)

    predictions = []
    references = []
    for _, batch in enumerate(tqdm(dataloader)):
        outputs = model.generate(
            input_ids=batch['input_ids'].to(device),
            max_length=conf.tgt_len,
            attention_mask=batch['attention_mask'].to(device)
        )
        with tokenizer.as_target_tokenizer():
            outputs = [tokenizer.decode(out, clean_up_tokenization_spaces=False, skip_special_tokens=True) for out in
                       outputs]
            labels = np.where(batch['labels'] != -100, batch['labels'], tokenizer.pad_token_id)
            actuals = [tokenizer.decode(out, clean_up_tokenization_spaces=False, skip_special_tokens=True) for out in
                       labels]
            predictions.extend(outputs)
            references.extend(actuals)

    # results = metrics.compute(predictions=predictions, references=references)
    logger.info('-----:----- Dumping results -----:-----')
    with open(os.path.join(conf.checkpoint, 'results.json'), 'w', encoding='utf-8') as f:
        json.dump({'predictions': predictions, 'references': references}, f, ensure_ascii=False, indent=2)

    return {'predictions': predictions, 'references': references}


def preprocess_and_train(
        conf: Config,
        model: AutoModelForSeq2SeqLM,
        tokenizer: AutoTokenizer,
        train_set: Dataset,
        val_set: Dataset,
        test_set: Dataset
):
    def preprocess_function(examples):
        inputs = tokenizer(examples["input_seq"], max_length=conf.src_len, truncation=True, padding=True)
        labels = tokenizer(examples["output_seq"], max_length=conf.tgt_len, truncation=True, padding=True)
        inputs['input_ids'] = inputs['input_ids']

        inputs['labels'] = labels['input_ids']

        return inputs

    logger.info('-----:----- Tokenizing datasets -----:-----')
    tokenized_train = train_set.map(preprocess_function, batched=True, remove_columns=['input_seq', 'output_seq'],
                                    num_proc=conf.num_proc)
    tokenized_val = val_set.map(preprocess_function, batched=True, remove_columns=['input_seq', 'output_seq'],
                                num_proc=conf.num_proc)
    tokenized_test = test_set.map(preprocess_function, batched=True, remove_columns=['input_seq', 'output_seq'],
                                  num_proc=conf.num_proc)

    with open(os.path.join(conf.checkpoint, 'origin_refs.json'), 'w', encoding='utf-8') as f:
        json.dump({'references': test_set['output_seq']}, f, ensure_ascii=False, indent=2)

    data_collator = DataCollatorForSeq2Seq(tokenizer, model=model, return_tensors="pt")

    training_args = Seq2SeqTrainingArguments(
        output_dir=conf.checkpoint,
        do_train=True,
        do_eval=True,
        logging_strategy='steps',
        log_level='debug',
        logging_steps=250,
        num_train_epochs=conf.num_epochs,
        learning_rate=conf.lr,
        warmup_ratio=conf.warmup_ratio,
        weight_decay=conf.weight_decay,
        per_device_train_batch_size=conf.per_device_train_batch_size,
        per_device_eval_batch_size=conf.per_device_eval_batch_size,
        predict_with_generate=True,
        group_by_length=True,
        eval_steps=250,
        evaluation_strategy='steps',
        save_strategy="steps",
        save_steps=50,
        save_total_limit=1,
        gradient_accumulation_steps=4,
        report_to='none',
        label_names=['labels']
    )
    trainer = Seq2SeqTrainer(
        model=model,
        tokenizer=tokenizer,
        args=training_args,
        train_dataset=tokenized_train,
        data_collator=data_collator,
        eval_dataset=tokenized_val
    )
    logger.info('Training')
    trainer.train()

    outputs = compute_metric(conf=conf, model=model, tokenizer=tokenizer, tokenized_test=tokenized_test)

    return outputs


def main(model_name: str = 'vit5'):
    conf = Config(model_name)
    os.makedirs(f'{conf.checkpoint}', mode=0o777, exist_ok=True)
    logger.info(f'Load tokenizer and model checkpoint: {conf.pretrained_model_name_or_path}')
    tokenizer = AutoTokenizer.from_pretrained(conf.pretrained_model_name_or_path)
    model = AutoModelForSeq2SeqLM.from_pretrained(conf.pretrained_model_name_or_path)

    train_dataset, dev_dataset, test_dataset = prepare_data(conf=conf)

    outputs = preprocess_and_train(
        conf=conf,
        model=model,
        tokenizer=tokenizer,
        train_set=train_dataset,
        val_set=dev_dataset,
        test_set=test_dataset
    )
    print(outputs[0])
    with open(f'{conf.checkpoint}/results.json', 'r', encoding='utf-8') as f:
        data = json.load(f)

    pred = data['predictions']
    ref = data['references']

    bleu_score = bleu(pred, ref)

    metrics = load_metric('rouge')
    rouge_results = [{k: (v.mid.fmeasure) * 100} for k, v in metrics.compute(predictions=pred, references=ref).items()]

    meteor_metrics = evaluate.load('meteor')
    meteor_results = meteor_metrics.compute(predictions=pred, references=ref)

    bert_score = evaluate.load('bertscore')
    bert_results = bert_score.compute(predictions=pred, references=ref, lang='vi')
    bert_mean_f1 = np.array(bert_results['f1']).mean()

    results = {
        "bleu_score": bleu_score,
        "rouge_results": rouge_results,
        "meteor_results": meteor_results,
        "bert_score_mean_f1": bert_mean_f1
    }

    print(results)

    with open(f'{conf.checkpoint}/scores.json', 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

if __name__ == '__main__':
  main()