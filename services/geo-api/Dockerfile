FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE 5002
CMD ["gunicorn", "--timeout", "900", "-w", "1", "-b", "0.0.0.0:5002", "app:app"]
