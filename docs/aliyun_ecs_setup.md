# Running the report agent on Aliyun ECS (阿里云 ECS)

This guide sets up a pay-as-you-go (按量付费) Aliyun server with a GPU,
installs Ollama and a Qwen model, runs the demo, and shuts everything down
afterwards so it stops costing money.

> **Data rule.** Only the synthetic demo dataset goes on this server. Never
> upload customer data or the Lending Club files from your PC. The cloud box is
> only for speed testing and demos; at a customer, the same code runs on their
> own server.

> **Unverified.** Instance type names, prices, menu labels and the ModelScope
> commands below come from documentation, not from a test run in this
> project. Check them on the console before you pay. Prices change often and
> differ by region.

## 0. Before you start

- An Aliyun account with real-name verification (实名认证) completed.
- Some balance in the account. Pay-as-you-go instances usually require a
  minimum balance (often ¥100). *Unverified.*
- Your public IP, for the firewall rule. Open https://www.ip.cn or search
  "我的IP" and note the address.
- On Windows, the built-in `ssh` command (PowerShell) is enough.

Optional but recommended: set a budget alert in 费用与成本 → 预算管理.

## 1. Pick an instance

Two options. GPU is much faster; CPU is closer to a customer's ordinary server.

| Purpose | Suggested type (*unverified*) | Spec | Rough price (*unverified*) |
|---|---|---|---|
| GPU, recommended | `ecs.gn7i-c8g1.2xlarge` | 8 vCPU, 30 GiB RAM, 1 × NVIDIA A10 24 GB | about ¥10–15 per hour |
| CPU only | `ecs.g7.4xlarge` or `ecs.g8i.4xlarge` | 16 vCPU, 64 GiB RAM | about ¥3–5 per hour |

If the GPU type is sold out in one zone, try another zone (可用区) or another
mainland region. Spot instances (抢占式实例) can be much cheaper but may be
reclaimed during a demo, so avoid them for anything you show to a client.

Which model fits:

| Hardware | Model to try first | Approx. size (Q4) |
|---|---|---|
| A10 24 GB GPU | `qwen3:14b`, or `qwen3:30b-a3b` (MoE, ~3B active) | ~9 GB / ~19 GB |
| 64 GB CPU | `qwen3:30b-a3b` (fast on CPU because only ~3B params are active) | ~19 GB |
| Your 32 GB laptop | `qwen3:8b`, then try `qwen3:30b-a3b` | ~5 GB / ~19 GB |

If a newer Qwen release is on ModelScope when you read this, it is worth
trying; the code only needs the model name.

## 2. Create the server (控制台 → 云服务器 ECS → 创建实例)

1. **付费模式 (Billing):** 按量付费 (pay-as-you-go).
2. **地域 (Region):** a mainland region close to you, e.g. 华东1（杭州）,
   华东2（上海）, or 华北2（北京）. Mainland regions reach ModelScope
   quickly.
3. **实例规格 (Instance type):** search for the type from step 1.
4. **镜像 (Image):** 公共镜像 → Ubuntu 22.04 64-bit. For a GPU type, tick
   the option to install the GPU driver automatically (安装GPU驱动 / 自动安装
   GPU 驱动), choosing the newest CUDA version offered. *The label varies.*
5. **系统盘 (Disk):** ESSD, 100 GB (models are large).
6. **公网 IP (Public IP):** tick 分配公网IPv4地址, billed by traffic
   (按使用流量), bandwidth 10–100 Mbps. Downloads from ModelScope inside
   Aliyun are fast, but the public bandwidth limits your SSH session only.
7. **安全组 (Security group):** create a new one. Add only this inbound rule:
   - Protocol TCP, port **22**, source **`<your IP>/32`**.

   Do **not** open port 11434 (Ollama) to the internet. Ollama has no login;
   anyone who can reach it can use it. Use an SSH tunnel instead (step 7).
8. **登录凭证 (Login):** 密钥对 (key pair). Create one and download the
   `.pem` file. Keep it safe.
9. **自动释放 (Auto release):** if offered, set a release time a few hours
   after your demo, as a safety net.
10. Confirm and create. Note the public IP.

## 3. Connect

From PowerShell on your laptop:

```powershell
# Restrict the key file to your user first (Windows OpenSSH refuses loose permissions)
icacls .\my-key.pem /inheritance:r /grant:r "$($env:USERNAME):R"
ssh -i .\my-key.pem root@<public-ip>
```

On Ubuntu images the user may be `root` or `ecs-user`, depending on the
image. Then check the GPU:

```bash
nvidia-smi        # should list one A10 with ~24 GB; skip on CPU instances
```

If `nvidia-smi` is missing, the automatic driver install may still be
running. Wait ten minutes and try again, or see Aliyun's guide
"为GPU实例安装GPU驱动".

## 4. Install Ollama

The official script downloads from GitHub, which can be slow or fail from
mainland China:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

If that stalls, use one of these instead:

- **ModelScope copy (*unverified*):** ModelScope hosts Ollama's Linux build.
  ```bash
  pip install -U modelscope
  modelscope download --model=modelscope/ollama-linux --local_dir ./ollama-linux
  cd ollama-linux && chmod +x ollama-modelscope-install.sh && ./ollama-modelscope-install.sh
  ```
  Read the model page on modelscope.cn first: it names the install script
  and lets you pick a version with `--revision`.
- **Copy from your laptop:** download `ollama-linux-amd64.tgz` from the
  Ollama GitHub releases page on a fast connection, then
  `scp -i .\my-key.pem .\ollama-linux-amd64.tgz root@<ip>:/root/` and
  `sudo tar -C /usr -xzf ollama-linux-amd64.tgz`, then start it with
  `ollama serve &`.

Check that it runs and only listens locally:

```bash
ollama --version
curl http://127.0.0.1:11434/api/tags
```

The service listens on `127.0.0.1:11434` by default. Keep it that way.

## 5. Download the model from ModelScope (魔搭)

Ollama's own registry can be slow in China. ModelScope mirrors Qwen's GGUF
files and lets Ollama pull them directly (*unverified for each model; check
the repo exists on modelscope.cn first*):

```bash
ollama pull modelscope.cn/Qwen/Qwen3-14B-GGUF:Q4_K_M
# or: ollama pull modelscope.cn/Qwen/Qwen3-8B-GGUF:Q4_K_M
# or: ollama pull modelscope.cn/Qwen/Qwen3-30B-A3B-GGUF:Q4_K_M

ollama list
```

Give it a short name so the settings stay readable:

```bash
ollama cp modelscope.cn/Qwen/Qwen3-14B-GGUF:Q4_K_M qwen3:14b
```

Fallback if the direct pull does not work: download the `.gguf` file and
build the model yourself.

```bash
pip install -U modelscope
modelscope download --model Qwen/Qwen3-14B-GGUF Qwen3-14B-Q4_K_M.gguf --local_dir /root/models
printf 'FROM /root/models/Qwen3-14B-Q4_K_M.gguf\n' > Modelfile
ollama create qwen3:14b -f Modelfile
```

A model built from a bare GGUF file can come out with the wrong chat
template. If replies look garbled or never stop, prefer the direct pull.

## 6. Get the code and run a report

GitHub can also be slow from mainland servers. If `git clone` stalls, zip
the repo on your laptop and copy it with `scp`.

```bash
apt-get update && apt-get install -y python3-venv git
git clone https://github.com/dengdengkenya/ai-agent-demo-proj.git
cd ai-agent-demo-proj
python3 -m venv .venv
# Aliyun's PyPI mirror is much faster inside Aliyun
.venv/bin/pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

cat > .env <<'EOF'
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3:14b
EOF

.venv/bin/python -m report_agent build-data     # synthetic data only
.venv/bin/python -m report_agent check          # confirms the model answers, prints speed
.venv/bin/python -m report_agent report --team risk
.venv/bin/python -m report_agent report --team marketing --request "重点看各渠道ROI和短信召回效果"
```

Each run prints a timing table (model load, prompt tokens/s, output
tokens/s, total). Run the same commands on your laptop to compare. Reports
are saved under `outputs/reports/`. Copy one back with:

```powershell
scp -i .\my-key.pem root@<ip>:/root/ai-agent-demo-proj/outputs/reports/*.md .
```

## 7. Optional: use the cloud model from your laptop

To run the code on your laptop but use the faster cloud model, open an SSH
tunnel instead of opening port 11434:

```powershell
ssh -i .\my-key.pem -N -L 11435:127.0.0.1:11434 root@<public-ip>
```

Leave that window open, then on the laptop set `OLLAMA_HOST=http://127.0.0.1:11435`
and `OLLAMA_MODEL=qwen3:14b` and run the agent as usual. The prompts (which
contain the summary tables) then travel to the cloud server through the
encrypted tunnel, so do this with the synthetic data only.

## 8. After the demo: stop or release

Pay-as-you-go charges continue while the instance exists.

- **停止 (Stop)** in 节省停机模式 (economical mode): compute and GPU charges
  stop, but the disk (and a fixed EIP, if you used one) are still billed.
  Downloaded models stay on the disk. *Caveat:* a stopped GPU instance may
  fail to start again if that zone runs out of GPUs.
- **释放 (Release):** deletes the instance and its system disk. All charges
  stop. You will need to repeat steps 2–6 next time (~30–60 minutes,
  mostly the model download).

Recommendation: release after each demo unless the next one is within a day
or two. Afterwards, check 费用与成本 → 账单详情 the next day to confirm
nothing is still running.

To make the next setup quicker, you can create a custom image (创建自定义镜像)
from the instance before releasing it. Images are billed for snapshot storage,
which is small compared with a running GPU.

## Moving to a customer's server

Nothing in the code changes. Install Ollama on their server (offline: copy
the Ollama build and the `.gguf` model file on a USB drive or via their file
transfer process) and set `OLLAMA_HOST` and `OLLAMA_MODEL` in `.env`. The
analyses currently read the synthetic SQLite database; connecting them to a
customer's MySQL tables is a separate step. No component of the agent calls
an external service.
