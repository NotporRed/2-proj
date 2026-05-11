FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static ./static
EXPOSE 5000
CMD ["gunicorn", "--timeout", "900", "-w", "2", "-b", "0.0.0.0:5000", "app:app"]
