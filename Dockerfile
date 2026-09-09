FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv

RUN useradd -m -u 10001 hermes && mkdir -p /var/log/hermes && chown hermes /var/log/hermes

# libGL and libglib for opencv, which rapidocr imports in every stage of the OCR
# pipeline (ch_ppocr_det, _rec, _cls). Neither is in python-slim, and without
# them `import cv2` fails at startup and /v1/readability answers 503 for the life
# of the container. Nothing else in Hermes needs them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the OCR models into the image — 31MB across detection, recognition and
# the orientation classifier.
#
# NOT optional. RapidOCR downloads them into site-packages on FIRST USE, which
# fails twice over at runtime: site-packages is root-owned and the process runs
# as `hermes`, and a container that has to reach the internet before it can
# answer is a container that breaks in an airgapped or rate-limited environment.
# Downloading here, as root, at build time, makes the image self-contained.
#
# All three, even though `Global.use_cls` is false: RapidOCR's constructor builds
# every stage regardless of the flags, so a missing cls model still fails.
RUN python -c "from rapidocr import RapidOCR; RapidOCR()" \
    && chmod -R a+rX "$(python -c 'import rapidocr,os;print(os.path.dirname(rapidocr.__file__))')/models"

COPY app ./app
COPY config ./config

USER hermes
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import httpx;httpx.get('http://localhost:8080/healthz',timeout=4).raise_for_status()"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
