FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY clinical_decision_room ./clinical_decision_room
RUN pip install --no-cache-dir .

COPY . .

EXPOSE 9999

CMD ["python", "-m", "clinical_decision_room"]
