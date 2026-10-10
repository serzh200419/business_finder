FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
