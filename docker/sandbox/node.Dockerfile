FROM node:20.18-alpine3.20@sha256:3488b10bf958af7125a176419d2d8a9937d895bf124012aae811651988d2ffe6
RUN npm install -g --no-fund --no-audit jest@29.7.0 && npm cache clean --force
ENV NODE_PATH=/usr/local/lib/node_modules HOME=/tmp
USER 65534:65534
