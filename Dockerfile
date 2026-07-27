FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# collectstatic imports settings; the DEBUG/SECRET_KEY guard would trip with no env at
# build time. Provide throwaway build-only values — they are NOT baked into runtime env
# (real values come from compose env_file at run time).
RUN DEBUG=1 SECRET_KEY=build-only python manage.py collectstatic --noinput
EXPOSE 8000
# One worker is mandatory (I1). config/asgi.py reads UVICORN_WORKERS to assert it.
ENV UVICORN_WORKERS=1
CMD ["sh","-c","python manage.py migrate --noinput && \
     exec uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-1}"]
