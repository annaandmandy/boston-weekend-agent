FROM public.ecr.aws/lambda/python:3.11

COPY requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: tiktoken==0.7.0
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY daily_social.py ${LAMBDA_TASK_ROOT}/daily_social.py
COPY persona.json ${LAMBDA_TASK_ROOT}/persona.json
COPY memory.default.json ${LAMBDA_TASK_ROOT}/memory.default.json

CMD ["daily_social.lambda_handler"]
