FROM python:3.14-slim

RUN useradd --uid 10001 --create-home runner
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY run.py .
COPY prospector/ prospector/
COPY sql/ sql/

USER runner
EXPOSE 8080
CMD ["python", "run.py", "--serve"]
