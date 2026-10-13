FROM node:22-slim

ENV NODE_ENV=production \
    DATA_DIR=/data

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin archive \
    && mkdir -p /data/whatsapp-auth /data/whatsapp-events /data/media/whatsapp \
    && chown -R archive:archive /data
COPY index.mjs message-metadata.mjs test-message-metadata.mjs ./
RUN npm test && rm test-message-metadata.mjs

USER archive
EXPOSE 8765
CMD ["node", "index.mjs"]