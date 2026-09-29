FROM python:3.12-slim

# git CLI (co subprocess trong app/services/git_service.py) + openssh-client
# de git clone/pull/push qua SSH voi credential duoc mount vao container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/sh appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .

COPY bin/run /bin/run
RUN chmod +x /bin/run

RUN mkdir -p /mnt/secrets /app/data && chown -R appuser:appuser /app /mnt/secrets

USER appuser
ENV HOME=/home/appuser

EXPOSE 8000

CMD ["/bin/run"]
