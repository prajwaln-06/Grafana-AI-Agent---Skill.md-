FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY skills/ skills/

ENV SKILLS_ROOT=/srv/skills
EXPOSE 8000

CMD ["python", "run_server.py"]
