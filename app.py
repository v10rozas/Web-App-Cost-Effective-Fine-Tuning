### ----------------------------------------------------------------------- ###
### ----------------------------- BIBLIOTECAS ----------------------------- ###
### ----------------------------------------------------------------------- ###
import os
import sys
import torch
import pandas as pd
import subprocess

from flask import Flask, request, render_template, session, flash, redirect, url_for, jsonify
import logging as py_logging
import ngrok
from celery_utils import celery_init_app

from datasets import load_dataset, Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, HfArgumentParser, TrainingArguments, logging
from peft import LoraConfig, PeftModel, get_peft_model
from trl import SFTTrainer

from transformers import pipeline
from langchain_community.llms.huggingface_pipeline import HuggingFacePipeline
from langchain_core.prompts import PromptTemplate
from langchain.chains import LLMChain, ConversationChain
from langchain.memory import ConversationBufferMemory

### ----------------------------------------------------------------------- ###
### -------------------- CONFIGURACION DE LA APP FLASK -------------------- ###
### ----------------------------------------------------------------------- ###
app = Flask(__name__, template_folder='./templates')

app.config.from_mapping(
    CELERY=dict(
        broker_url="redis://localhost:6379",
        result_backend="redis://localhost:6379",
        task_ignore_result=True,
    ),
)
celery = celery_init_app(app)

activate_listener = 0

if len(sys.argv) > 1:
    try:
        activate_listener = int(sys.argv[1])
    except ValueError:
        print("El valor de activate_listener debe ser un número entero.")

if (activate_listener == 1):
    os.environ['NGROK_AUTHTOKEN'] = 'NGROK_AUTHTOKEN_HERE'
    py_logging.basicConfig(level=py_logging.INFO)
    listener = ngrok.werkzeug_develop()

tokenizer = None
model = None
pipe = None
llm = None
memory = None
template_conversation = None
prompt_conversation = None
conversation = None

def adjust_csv(row, name_col_question, name_col_answer):
  """ Esta funcion se encarga de ajustar el dataset enviado por el usuario al
  formato csv adecuado para poder convertir el csv en una cadena de texto. El
  csv de salida cuenta con una unica columna, nombrada 'text', donde cada
  muestra contiene los prompts de las diferentes preguntas y respuestas que se
  utilizan para entrenar el modelo. """

  boundary = "--boundary--"
  prompt = "Below is an instruction that describes a task paired with input that provides further context. Write a response that appropriately completes the request."
  instruction = "Answer the following question."
  question = str(row[name_col_question])
  answer = str(row[name_col_answer])

  text = boundary + prompt + "\n\n### Instruction:\n" + instruction + "\n\n### Input:\n" + question + "\n\n### Response:\n" + answer + "</s>"

  adjust_ds_csv = pd.Series([text])

  return adjust_ds_csv

def csv_to_string(ds):
  """ Esta funcion se encarga de convertir el csv introducido por el usuario a
  una unica cadena de texto. """

  adjust_ds_csv = ds[[ds.columns[0],ds.columns[1]]].apply(adjust_csv, args=(ds.columns[0], ds.columns[1]), axis=1)

  list_of_samples_str = adjust_ds_csv.iloc[:, 0].astype(str)
  adjust_ds_string = ' '.join(list_of_samples_str)

  return adjust_ds_string

def string_to_csv(adjust_ds_string):
  """ Esta funcion se encarga de convertir el dataset en formato texto a
  formato csv. """

  samples = adjust_ds_string.split("--boundary--")[1:]
  ds_from_strings = pd.DataFrame(samples, columns=['text'])

  return ds_from_strings

### ----------------------------------------------------------------------- ###
### --------------------- FUNCIONAMIENTO DE LA APP WEB -------------------- ###
### ----------------------------------------------------------------------- ###
@celery.task(bind=True)
def fine_tune_llm(self, adjust_ds_string, fine_tuned_model_name):
    """ Esta funcion se encarga del fine-tuning. Mientras se realiza el ajuste,
    se envian mensajes sobre el estado del proceso al cliente. """


    message = "Preparando el ajuste..."
    self.update_state(state='PROGRESS', meta={'current': 0, 'total': 100, 'status': message})


    message = "Adaptando el conjunto de datos..."
    self.update_state(state='PROGRESS', meta={'current': 10, 'total': 100, 'status': message})

    ds_from_strings = string_to_csv(adjust_ds_string)
    ds = Dataset.from_pandas(ds_from_strings)


    message = "Descargando el modelo desde Hugging Face..."
    self.update_state(state='PROGRESS', meta={'current': 30, 'total': 100, 'status': message})

    model_name = "TinyPixel/Llama-2-7B-bf16-sharded"

    compute_dtype = getattr(torch, "float16")

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=compute_dtype,
        bnb_4bit_quant_type="nf4",
    )

    model = AutoModelForCausalLM.from_pretrained(
      model_name,
      quantization_config=bnb_config,
      device_map={"": 0},
    )
    model.config.use_cache = False
    model.config.pretraining_tp = 1

    tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    peft_params = LoraConfig(
      r=64,
      lora_alpha=64,
      lora_dropout=0.1,
      bias="none",
      task_type="CAUSAL_LM",
    )


    message = "Ajustando los parámetros..."
    self.update_state(state='PROGRESS', meta={'current': 40, 'total': 100, 'status': message})

    training_params = TrainingArguments(
      output_dir="./tmp/results",
      num_train_epochs=3,
      per_device_train_batch_size=4,
      gradient_accumulation_steps=1,
      optim="paged_adamw_32bit",
      save_steps=0,
      logging_steps=5,
      learning_rate=5e-5,
      weight_decay=0.001,
      fp16=False,
      bf16=False,
      max_grad_norm=0.3,
      max_steps=-1,
      warmup_ratio=0.03,
      group_by_length=True,
      lr_scheduler_type="linear",
      report_to="tensorboard",
    )

    trainer = SFTTrainer(
      model=model,
      tokenizer=tokenizer,
      args=training_params,
      peft_config=peft_params,
      train_dataset=ds,
      dataset_text_field="text",
      max_seq_length=None,
      packing=False,
    )

    trainer.train()
    trainer.model.save_pretrained("./tmp/new_model")
    trainer.tokenizer.save_pretrained("./tmp/new_model")

    del model
    del tokenizer
    del trainer
    torch.cuda.empty_cache()


    message = "Combinando los parámetros..."
    self.update_state(state='PROGRESS', meta={'current': 70, 'total': 100, 'status': message})

    tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    base_model = AutoModelForCausalLM.from_pretrained(
      model_name,
      low_cpu_mem_usage=True,
      return_dict=True,
      torch_dtype=torch.float16,
      device_map={"": 0},
    )

    model = PeftModel.from_pretrained(base_model, "./tmp/new_model")
    model = model.merge_and_unload()


    message = "Enviando el modelo ajustado a Hugging Face..."
    self.update_state(state='PROGRESS', meta={'current': 90, 'total': 100, 'status': message})

    HF_API_KEY = "HUGGING_FACE_API_KEY_HERE"
    subprocess.run(["huggingface-cli", "login", "--token", HF_API_KEY])
    model.push_to_hub(fine_tuned_model_name, max_shard_size="1000MB", private=True)
    tokenizer.push_to_hub(fine_tuned_model_name, private=True)


    message = "¡Entrenamiento completado!"
    self.update_state(state='FINISH', meta={'current': 100, 'total': 100, 'status': message, 'result': 1003})

    del model
    del tokenizer
    torch.cuda.empty_cache()
    comand = "rm -r ~/.cache/huggingface/hub/"
    subprocess.run(comand, shell=True)
    comand = "rm -r ./tmp"
    subprocess.run(comand, shell=True)


    return {'current': 100, 'total': 100, 'status': '¡Entrenamiento completado!', 'result': 1003}

@celery.task(bind=True)
def download_inference(self, model_name):
    """ Esta funcion se encarga de descargar el LLM ajustado y de preparar la
    comunicacion entre el usuario y el LLM. """

    global tokenizer, model, pipe, llm, memory, template_conversation, prompt_conversation, conversation


    message = "Conectando con Hugging Face..."
    self.update_state(state='PROGRESS', meta={'current': 0, 'total': 100, 'status': message})

    HF_API_KEY = "HUGGING_FACE_API_KEY_HERE"
    subprocess.run(["huggingface-cli", "login", "--token", HF_API_KEY])


    message = "Descargando el LLM elegido..."
    self.update_state(state='PROGRESS', meta={'current': 10, 'total': 100, 'status': message})

    tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        trust_remote_code=True,
        low_cpu_mem_usage=True,
        return_dict=True,
        torch_dtype=torch.float16,
        device_map={"": 0},
    )


    message="Preparando la comunicación con el LLM ajustado..."
    self.update_state(state='PROGRESS', meta={'current': 80, 'total': 100, 'status': message})

    pipe = pipeline("text-generation", model=model, tokenizer=tokenizer, max_new_tokens=200)
    llm = HuggingFacePipeline(pipeline=pipe, model_id=model_name, pipeline_kwargs={"max_new_tokens":200})
    memory = ConversationBufferMemory()
    template_conversation = """{history}{input}"""
    prompt_conversation = PromptTemplate(input_variables=["history", "input"], template=template_conversation)
    conversation = ConversationChain(
      prompt=prompt_conversation,
      llm=llm,
      verbose=False,
      memory=memory,
    )


    message="¡Modelo listo para recibir preguntas!"
    self.update_state(state='FINISH', meta={'current': 100, 'total': 100, 'status': message, 'result': 1001})


    return {'current': 100, 'total': 100, 'status': '¡Modelo listo para recibir preguntas!', 'result': 1001}

@celery.task(bind=True)
def run_inference(self, question):
    """ Esta funcion se encarga de adaptar las preguntas al formato adecuado
    y de generar las respuestas. """

    global conversation


    message = "Realizando la pregunta..."
    self.update_state(state='PROGRESS', meta={'current': 0, 'total': 100, 'status': message})

    template = """Below is an instruction that describes a task paired with input that provides further context. Write a response that appropriately completes the request.

    ### Instruction:
    Answer the following question.

    ### Input:
    {question}

    ### Response:
    """

    prompt = PromptTemplate(input_variables=["question"], template=template)
    input = prompt.format(question=question)
    full_response = conversation.predict(input=input)

    response_beginning = full_response.find("### Response:")
    response_end = full_response.find("</s>")
    response = full_response[response_beginning:response_end].replace("### Response:", "").strip()

    memory.clear()


    message = "¡Respuesta generada!"
    self.update_state(state='FINISH', meta={'current': 100, 'total': 100, 'status': message, 'result': response})


    return {'current': 100, 'total': 100, 'status': '¡Respuesta generada!', 'result': response}

@celery.task(bind=True)
def end_inference(self):
    """ Esta funcion se encarga de liberar los recursos del servidor cuando
    el usuario no quiere realizar mas preguntas. """

    global tokenizer, model, pipe, llm, memory, template_conversation, prompt_conversation, conversation


    message = "Cerrando la inferencia..."
    self.update_state(state='PROGRESS', meta={'current': 0, 'total': 100, 'status': message})

    del conversation
    del prompt_conversation
    del template_conversation
    del memory
    del llm
    del pipe
    del model
    del tokenizer
    torch.cuda.empty_cache()
    comand = "rm -r ~/.cache/huggingface/hub/"
    subprocess.run(comand, shell=True)


    message = "¡Inferencia finalizada!"
    self.update_state(state='FINISH', meta={'current': 100, 'total': 100, 'status': message, 'result': 1002})


    return {'current': 100, 'total': 100, 'status': '¡Inferencia finalizada!', 'result': 1002}


@app.route("/", methods=["GET"])
def main():
    """ Esta funcion carga la pagina principal. """
    return render_template("main.html")

@app.route("/run_inference_page.html", methods=["GET"])
def run_inference_page():
    """ Esta funcion carga la pagina para conversar con el modelo. """
    return render_template("run_inference_page.html")

@app.route("/downloadinference", methods=["POST"])
def downloadinference():
    """ Esta funcion se encarga de descargar el LLM ajustado y de preparar la
    comunicacion entre el usuario y el LLM. """
    model_name = str(request.form.get("llm_name"))
    task = download_inference.apply_async(args=[model_name])

    return jsonify({}), 202, {'Location': url_for('taskstatus', task_id=task.id)}

@app.route("/runinference", methods=["POST"])
def runinference():
    """ Esta funcion se encarga de adaptar las preguntas al formato adecuado
    y de generar las respuestas. """
    question = str(request.form.get("question"))
    task = run_inference.apply_async(args=[question])

    return jsonify({}), 202, {'Location': url_for('taskstatus', task_id=task.id)}

@app.route("/endinference", methods=["POST"])
def endinference():
    """ Esta funcion se encarga de liberar los recursos del servidor cuando
    el usuario no quiere realizar mas preguntas. """
    task = end_inference.apply_async()

    return jsonify({}), 202, {'Location': url_for('taskstatus', task_id=task.id)}

@app.route("/fine_tuning_page.html", methods=["GET"])
def fine_tune_page():
    """ Esta funcion carga la pagina para ajustar el modelo. """
    return render_template("fine_tuning_page.html")

@app.route("/finetunellm", methods=["POST"])
def finetunellm():
    """ Esta funcion se encarga del fine-tuning. Mientras se realiza el ajuste,
    se envian mensajes sobre el estado del proceso al cliente. """
    ds_received = request.files["dataset"]
    ds_csv = pd.read_csv(ds_received)
    if (len(ds_csv)>300):
      ds_csv = ds_csv.sample(n=300)
    adjust_ds_string = csv_to_string(ds_csv)
    new_model = str(request.form.get("llm_name"))
    task = fine_tune_llm.apply_async(args=[adjust_ds_string, new_model])

    return jsonify({}), 202, {'Location': url_for('taskstatus', task_id=task.id)}

@app.route("/status/<task_id>")
def taskstatus(task_id):
    """ Esta funcion envia actualizaciones sobre el estado de la tarea. """
    try:
      task = fine_tune_llm.AsyncResult(task_id)
    except:
      pass
    try:
      task = download_inference.AsyncResult(task_id)
    except:
      pass
    try:
      task = run_inference.AsyncResult(task_id)
    except:
      pass
    try:
      task = end_inference.AsyncResult(task_id)
    except:
      pass

    if task.state=='PENDING':
      response = {
        'state': task.state,
        'current': 0,
        'total': 1,
        'status': 'Iniciando...'
      }
    elif task.state=='FINISH':
      response = {
        'state': task.state,
        'current': task.info.get('current', 0),
        'total': task.info.get('total', 1),
        'status': task.info.get('status', ''),
        'result': task.info.get('result', ''),
      }
    elif task.state=='PROGRESS':
      response = {
        'state': task.state,
        'current': task.info.get('current', 0),
        'total': task.info.get('total', 1),
        'status': task.info.get('status', ''),
      }
    else:
      response = {
        'state': task.state,
        'current': 1,
        'total': 1,
        'status': str(task.info),
      }
    return jsonify(response)


if __name__ == "__main__":
    """ Esta funcion se encarga de ejecutar la aplicacion Flask. """
    app.run()
