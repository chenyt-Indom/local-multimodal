# vendor-assets —— 构建期用的离线资源

Dockerfile 会**优先**从这里取资源，取不到才回退到联网下载。
把文件放进来，`docker build` 就完全不依赖网络（几秒钟过）。

## code-server-linux-amd64.tar.gz

容器里「🧩 VS Code」按钮用的真 VS Code（code-server）。
**默认不提交到版本库**（229MB，且 GitHub 单文件限 100MB），请自行下载：

```bash
# 国内建议走代理，直连 github 会 302 到 objects.githubusercontent.com，经常挂住
curl -fL -o code-server-linux-amd64.tar.gz \
  "https://gh-proxy.com/https://github.com/coder/code-server/releases/download/v4.137.0/code-server-4.137.0-linux-amd64.tar.gz"
```

版本要跟 Dockerfile 里的 `ARG CS_VER` 一致。

> 为什么不放 `docker/` 根下直接 COPY：`COPY docker/xxx.tar.gz` 在文件不存在时
> 会让**整个构建失败**。改成 COPY 一个目录（总存在），再用 shell 判断有没有。
