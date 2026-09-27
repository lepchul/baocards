FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY core.py texts.py bot.py backup.py ./
RUN useradd --uid 10001 --create-home bao && mkdir /app/data && chown bao:bao /app/data
USER bao
CMD ["python", "bot.py"]
