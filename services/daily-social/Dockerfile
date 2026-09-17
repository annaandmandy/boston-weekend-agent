FROM public.ecr.aws/lambda/python:3.11

COPY requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: tiktoken==0.7.0
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY daily_social.py ${LAMBDA_TASK_ROOT}/daily_social.py

CMD ["daily_social.lambda_handler"]
