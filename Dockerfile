FROM python:3.12-slim

# The data (Comprehensive Rules index ~1 MB + cards.db ~30 MB) is baked
# into the image at build time — NOT committed to git. The build runs the
# normal refresh pipeline (network available in the build stage), so the
# running container never needs network for lookups and stays consistent
# with the release it shipped with. Redeploy to refresh the data.
WORKDIR /app
COPY pyproject.toml README.md ./
COPY mtgjudge ./mtgjudge
COPY tests ./tests

# Secrets never enter the image: MISTRAL_API_KEY / WEB_TOKEN are provided
# by the host (Render env vars, or docker run -e).
ENV MTG_DATA_DIR=/data PYTHONUNBUFFERED=1
RUN python -m mtgjudge refresh

EXPOSE 10000
CMD ["python", "-m", "mtgjudge", "serve", "--host", "0.0.0.0"]
