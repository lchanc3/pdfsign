FROM python:3.12-slim

# Install font: TW-Kai
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-cns11643-kai \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pdfsign.py .

ENV PDFSIGN_PORT=8080
EXPOSE 8080
CMD ["python", "pdfsign.py"]
