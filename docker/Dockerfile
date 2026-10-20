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
# 跟 install.sh 的 systemd 設定一樣：壓住 glibc 的記憶體池數，用完的記憶體才還得回去
ENV MALLOC_ARENA_MAX=2
EXPOSE 8080
CMD ["python", "pdfsign.py"]
