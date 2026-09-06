FROM python:3.12-slim
WORKDIR /app
# requirements.txt is copied for reference only — the INSTALL comes from the
# hash-locked closure. requirements.txt pins VERSIONS; the lock additionally
# pins CONTENT (SHA-256 per distribution) across the full transitive closure,
# so a substituted artifact at a pinned version fails the build (SEC-3).
#
# ONE lock covers both architectures here, unlike the NPCI backend which needs
# one per arch — that split is forced by torch's amd64-only NVIDIA CUDA deps,
# and this service has no torch. Verified, not assumed: --require-hashes resolves
# on linux/arm64 AND linux/amd64. Adding a platform-conditional dependency would
# break that, and the fix is to split the lock per arch as the NPCI backend does.
#
# Editing requirements.txt alone now has NO effect on the image — regenerate the
# lock (command in its header). scripts/ci/hygiene-check.sh fails the commit when
# a pin here is missing from the lock.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY . .

RUN adduser --disabled-password --no-create-home --gecos "" appuser \
    && chown -R appuser /app
USER appuser

EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
