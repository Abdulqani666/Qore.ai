FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer fonts-dejavu fonts-liberation poppler-utils fontconfig \
    && rm -rf /var/lib/apt/lists/*
RUN printf '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig><alias binding="same"><family>Tahoma</family><prefer><family>DejaVu Sans</family></prefer></alias><alias binding="same"><family>Verdana</family><prefer><family>DejaVu Sans</family></prefer></alias><alias binding="same"><family>Times New Roman</family><prefer><family>Liberation Serif</family></prefer></alias><alias binding="same"><family>Arial</family><prefer><family>Liberation Sans</family></prefer></alias></fontconfig>' > /etc/fonts/local.conf && fc-cache -f
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=10000
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 180
