# 食品风险批次追召

本项目保存食品风险批次追召所需的领域上下文和校验契约，便于服务端功能围绕真实业务参与方展开。当前版本只提供资料读取、结构校验和命令行摘要，数据均为演示用虚构内容。

## 参与方

市场监管人员、生产企业、中间商、平台渠道

## 事实资料

- 报道披露假核桃油以普通大豆油灌装并贴不同标签销售
- 抽检发现棕榈酸含量异常并启动核查处置
- 案件需要从直播销售追溯到偏僻民房生产窝点和多级分成链

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 编译

```bash
python3 -m compileall -q src tests
```

## 命令行检查

```bash
python3 -m src.food_recall.context fixtures/context.json
```
