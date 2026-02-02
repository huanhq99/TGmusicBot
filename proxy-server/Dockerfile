FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir flask requests

COPY app.py .

ENV PORT=8899

EXPOSE 8899

CMD ["python", "app.py"]
