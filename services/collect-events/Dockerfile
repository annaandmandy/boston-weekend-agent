FROM public.ecr.aws/lambda/python:3.11

COPY requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY collect_events.py ${LAMBDA_TASK_ROOT}/collect_events.py

CMD ["collect_events.lambda_handler"]
