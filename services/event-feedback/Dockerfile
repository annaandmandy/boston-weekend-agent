FROM public.ecr.aws/lambda/python:3.11

COPY feedback.py ${LAMBDA_TASK_ROOT}/feedback.py

CMD ["feedback.lambda_handler"]
