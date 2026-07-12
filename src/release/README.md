# 半开源 / 保护核心代码 运行手册

MetaAgent 采用 **open-core** 模式:公开外壳(UI、启动器、`tools/`、`runtime/` 片段)
可自由开源;codegen 引擎与 AI 设计器逻辑编译成 `.pyd` 后不带源码分发。

## 为什么不搬文件、不改 import

`import graph_codegen` 会优先解析同名的 `graph_codegen.pyd`。Nuitka `--module`
产出的 `.pyd` 是 **原地直接替换品**,所以全仓库 100+ 处 `import` 一行都不用改,
测试全绿。我们只做"原地编译 + 备份原文件",完全可回滚。

## 能藏 / 不能藏(见 `core_manifest.py`)

- **能藏(CORE)**:`graph_model` `graph_codegen` `codegen` `patterns` `estimation`
  `designer_agent` `design_assistant` `coding_agent` —— 只在设计器内运行,不进产物。
- **藏不住(INLINED)**:`graph_codegen_templates`、`codegen_templates`、`runtime/*.py`
  —— 它们的**源码文本会被逐字内联进每个生成的 agent**,是产物的一部分,天生公开。
  别在这些文件上浪费混淆精力。

## 操作步骤

```powershell
pip install nuitka                          # 需要 C 编译器,Windows 上 Nuitka 会自动拉 MinGW
python release/protect_core.py status       # 看分类和当前状态(无需 nuitka)
python release/protect_core.py build        # 编译 CORE -> .pyd,原 .py 移入 release/_py_backup
python release/protect_core.py verify       # 在 .pyd 版本上跑回归,证明行为一致
python release/protect_core.py restore      # 还原:.py 复位,删除 .pyd
```

`build` 出错会中止且不动原文件;成功后原 `.py` 都在 `release/_py_backup/`,`restore` 一键回滚。

## 打包成 exe(保护后)

先 `build` 出 `.pyd`,再照常用 `MetaAgent.spec` 打包 —— 此时 PyInstaller 收进去的
是 `.pyd` 而非 `.py`,反编译无效。
> 注意:PyInstaller 本身**不是保护**(`pyinstxtractor` + 反编译几分钟破解),
> 保护来自 Nuitka 的 `.pyd`;两者叠加使用。

## License(法律护栏)

- 公开外壳:MIT / Apache-2.0(宽松,鼓励用);想威慑 SaaS 抄袭可用 AGPL-3.0。
- 核心 `.pyd`:配私有 EULA,明确禁止反编译/逆向/再分发。

## 想要授权/试用期/绑机

把 `protect_core.py` 里的 Nuitka 换成 PyArmor 即可获得 `--expired`(到期)、
`--bind-device`(绑机器)能力;分类与流程不变。

## 终极方案(商业化时)

把 `analyze()` / `generate_from_graph()` 和设计器 prompt 放到自己的服务器,
本地只上传 `graph.json`、下载生成代码。破解者永远拿不到引擎。
