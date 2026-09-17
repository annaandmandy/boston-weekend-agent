FROM public.ecr.aws/lambda/python:3.11

COPY requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: tiktoken==0.7.0
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY langchain_report.py ${LAMBDA_TASK_ROOT}/langchain_report.py

CMD ["langchain_report.lambda_handler"]
