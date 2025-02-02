#!/bin/bash
set -e

# Install deps to run the API in a seperate venv to isolate different components
conda create --name api-env -y python=3.10 pip
$HOME/.conda/envs/api-env/bin/pip install fastapi==0.109.2 uvicorn[standard]==0.27.0.post1 python-multipart==0.0.7 langchain==0.0.335 langchain-community==0.0.19 openai==1.11.1 unstructured[all-docs]==0.12.4 sentence-transformers==2.3.1 llama-index==0.9.44 dataclass-wizard==0.22.3 pymilvus==2.3.1 opencv-python==4.8.0.76 hf_transfer==0.1.5 text_generation==0.6.1  -i https://pypi.tuna.tsinghua.edu.cn/simple

# Install deps to run the UI in a seperate venv to isolate different components
conda create --name ui-env -y python=3.10 pip
$HOME/.conda/envs/ui-env/bin/pip install dataclass_wizard==0.22.2 gradio==4.15.0 jinja2==3.1.2 numpy==1.25.2 protobuf==3.20.3 PyYAML==6.0 uvicorn==0.22.0  -i https://pypi.tuna.tsinghua.edu.cn/simple

sudo -E apt-get update
sudo -E apt-get -y install ca-certificates curl
sudo -E install -m 0755 -d /etc/apt/keyrings
sudo -E curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo -E chmod a+r /etc/apt/keyrings/docker.asc

sudo -E echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo -E tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo -E apt-get update
sudo -E apt-get -y install docker-ce-cli

sudo -E /opt/conda/bin/pip install anyio==4.3.0 pymilvus==2.3.1  -i https://pypi.tuna.tsinghua.edu.cn/simple

sudo groupadd -g 1001 "docker-group"
sudo usermod -aG "docker-group" "workbench"


sudo -E mkdir /mnt/milvus
sudo -E mkdir /data
sudo -E chown workbench:workbench /mnt/milvus
sudo -E chown workbench:workbench /data