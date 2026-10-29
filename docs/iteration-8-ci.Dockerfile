FROM mcr.microsoft.com/playwright:v1.62.1-noble@sha256:dcc5531e97840b9b5e794f2814476b21571c5124a3fca2267d73041f56e7580e
RUN apt-get update -qq && apt-get install -y -qq python3-venv
COPY requirements.txt /tmp/guanlan-requirements.txt
RUN python3 -m venv /opt/guanlan-venv && /opt/guanlan-venv/bin/pip install -q -r /tmp/guanlan-requirements.txt
COPY package.json package-lock.json /opt/guanlan-node/
RUN cd /opt/guanlan-node && npm ci --no-audit --no-fund
ENV PYTHON=/opt/guanlan-venv/bin/python NODE_PATH=/opt/guanlan-node/node_modules
WORKDIR /work
RUN curl -fsSL https://nodejs.org/dist/v22.23.4/node-v22.23.4-linux-arm64.tar.xz -o /tmp/node22.tar.xz && python3 -c "import tarfile; tarfile.open('/tmp/node22.tar.xz').extractall('/opt', filter='data')"
ENV PATH=/opt/node-v22.23.4-linux-arm64/bin:$PATH
