"""
向量模型封装模块（本地 ONNX 推理）

选型：BAAI/bge-small-zh-v1.5 的 ONNX 版（来自 Xenova/bge-small-zh-v1.5）
  - 512 维 / INT8 量化后约 23MB / CPU 单条推理约 20ms
  - 采用 INT8 量化版，与项目「PaddleOCR → ONNX → INT8 量化」的技术路线完全一致
  - 注意：官方仓库 BAAI/bge-small-zh-v1.5 只有 PyTorch 格式，没有 ONNX，
    因此使用 Xenova 转换好的 ONNX 版本（含 INT8 量化）

为什么留在本地而不是调用云端 embedding API：
  保住项目「纯 CPU 运行、轻量化」的定位 —— 向量化不依赖联网，只有生成环节走云端。

降级设计（与项目 OCR 引擎三级降级一脉相承）：
  模型文件缺失或加载失败 → is_ready=False，上层自动退化为 BM25 单路检索，
  功能不整体失效，只在接口里标记降级状态。
"""
import json
import logging
import time
from pathlib import Path
from typing import List, Optional

import numpy as np

from app.config import EMBED_MODEL_DIR, EMBED_DIM, EMBED_MODEL_NAME

logger = logging.getLogger(__name__)

# 最大序列长度（BERT 位置编码上限）
MAX_SEQ_LEN = 512

# BGE 中文模型官方建议：**查询**前加指令前缀以提升检索效果（文档侧不加）
# 见 BAAI/bge-small-zh-v1.5 模型卡说明
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class EmbeddingModel:
    """
    本地 ONNX 向量模型封装。

    职责：加载模型与分词器 → 批量编码文本 → 池化 → L2 归一化
    """

    def __init__(self, model_dir: Path = EMBED_MODEL_DIR, use_int8: bool = True):
        self.model_dir = Path(model_dir)
        self.use_int8 = use_int8

        self._tokenizer = None
        self._session = None
        self._ready = False
        self._load_error: Optional[str] = None
        self._dim = EMBED_DIM

    # ---------- 加载 ----------

    def _resolve_onnx_path(self) -> Optional[Path]:
        """定位 ONNX 模型文件：优先 INT8 量化版，回退到全精度版"""
        candidates = []
        if self.use_int8:
            candidates.append(self.model_dir / "onnx" / "model_int8.onnx")
        candidates.append(self.model_dir / "onnx" / "model.onnx")
        for p in candidates:
            if p.exists():
                return p
        return None

    def load(self) -> bool:
        """
        加载模型与分词器。返回是否加载成功。
        失败时记录原因，不抛异常 —— 由上层决定是否降级。
        """
        if self._ready:
            return True

        try:
            from tokenizers import Tokenizer
            import onnxruntime as ort

            onnx_path = self._resolve_onnx_path()
            tok_path = self.model_dir / "tokenizer.json"

            if onnx_path is None:
                raise FileNotFoundError(
                    f"未找到 ONNX 模型文件，请检查 {self.model_dir / 'onnx'} 目录"
                )
            if not tok_path.exists():
                raise FileNotFoundError(f"未找到分词器文件: {tok_path}")

            t0 = time.time()
            self._tokenizer = Tokenizer.from_file(str(tok_path))
            self._tokenizer.enable_truncation(max_length=MAX_SEQ_LEN)
            self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

            opts = ort.SessionOptions()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(
                str(onnx_path), sess_options=opts, providers=["CPUExecutionProvider"]
            )

            # 从模型输出维度确认向量维度，避免与配置不一致
            out_shape = self._session.get_outputs()[0].shape
            if isinstance(out_shape[-1], int):
                self._dim = out_shape[-1]

            self._ready = True
            logger.info(
                f"向量模型加载成功: {EMBED_MODEL_NAME} "
                f"({'INT8量化' if 'int8' in onnx_path.name else '全精度'}, "
                f"{self._dim}维, 耗时 {time.time() - t0:.2f}s)"
            )
            return True

        except Exception as e:
            self._load_error = str(e)
            self._ready = False
            logger.warning(f"向量模型加载失败，将降级为 BM25 单路检索: {e}")
            return False

    # ---------- 编码 ----------

    @staticmethod
    def _cls_pool(last_hidden_state: np.ndarray) -> np.ndarray:
        """
        CLS 池化：取每个序列第一个 token 的向量。

        BGE 系列模型的官方池化方式就是 CLS（非 mean pooling），
        句子级语义被聚合到 [CLS] 位置。
        """
        return last_hidden_state[:, 0, :]

    @staticmethod
    def _l2_normalize(vectors: np.ndarray) -> np.ndarray:
        """L2 归一化：归一化后点积即余弦相似度，检索时省去计算模长"""
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)   # 防止除零
        return vectors / norms

    def encode(self, texts: List[str], is_query: bool = False,
               batch_size: int = 8) -> Optional[np.ndarray]:
        """
        批量编码文本为归一化向量。

        参数: texts     — 待编码文本列表
              is_query  — 是否为查询。查询会加 BGE 官方建议的指令前缀
                          （文档侧不加，这是 BGE 的推荐用法）
        返回: shape=(N, dim) 的 float32 数组（已 L2 归一化）；不可用时返回 None
        """
        if not self._ready and not self.load():
            return None
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)

        # BGE 建议：只在查询侧加指令前缀
        payload = [QUERY_INSTRUCTION + t if is_query else t for t in texts]

        all_vecs: List[np.ndarray] = []
        for start in range(0, len(payload), batch_size):
            batch = payload[start:start + batch_size]
            encodings = self._tokenizer.encode_batch(batch)

            input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
            attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
            token_type_ids = np.zeros_like(input_ids, dtype=np.int64)

            outputs = self._session.run(
                None,
                {
                    "input_ids": input_ids,
                    "attention_mask": attention_mask,
                    "token_type_ids": token_type_ids,
                },
            )
            pooled = self._cls_pool(outputs[0])
            all_vecs.append(pooled)

        vectors = np.vstack(all_vecs).astype(np.float32)
        return self._l2_normalize(vectors)

    def encode_one(self, text: str, is_query: bool = False) -> Optional[np.ndarray]:
        """编码单条文本，返回 shape=(dim,) 的归一化向量"""
        vecs = self.encode([text], is_query=is_query)
        if vecs is None or len(vecs) == 0:
            return None
        return vecs[0]

    # ---------- 状态 ----------

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_tag(self) -> str:
        """写入 document_chunk.embed_model 的模型标识，换模型时用于定向重建"""
        suffix = "int8" if self.use_int8 else "fp32"
        return f"{EMBED_MODEL_NAME}#{suffix}"

    def get_info(self) -> dict:
        """供 /api/rag/config 展示的模型状态"""
        return {
            "name": EMBED_MODEL_NAME,
            "dim": self._dim,
            "ready": self._ready,
            "quantized": self.use_int8,
            "model_tag": self.model_tag,
            "error": self._load_error,
        }


# ==================== 模块级单例 ====================

_model: Optional[EmbeddingModel] = None


def get_embedding_model() -> EmbeddingModel:
    """获取全局向量模型实例（懒加载，首次调用时加载）"""
    global _model
    if _model is None:
        _model = EmbeddingModel()
    return _model


def warmup() -> bool:
    """启动预热。失败不阻断服务启动，仅标记降级状态"""
    return get_embedding_model().load()


if __name__ == "__main__":
    # 自测：验证模型可加载、语义相似度是否符合直觉
    # 运行: .venv/Scripts/python.exe -m app.services.embedding
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    model = get_embedding_model()
    print("=" * 74)
    print("加载模型")
    print("=" * 74)
    ok = model.load()
    print(f"  加载结果: {'成功' if ok else '失败'}")
    print(f"  模型信息: {json.dumps(model.get_info(), ensure_ascii=False)}")

    if not ok:
        raise SystemExit("模型不可用，后续测试跳过")

    print()
    print("=" * 74)
    print("语义相似度验证（核心：语义相关应显著高于字面无关）")
    print("=" * 74)

    # 查询用"打车"这个口语词，文档里是"客运服务费"——字面完全不同
    query = "上个月打车花了多少钱"

    docs = [
        ("客运服务费 出租车 网约车 价税合计", "语义相关（字面不匹配）"),
        ("*运输服务*客运服务费 31.30 3%", "语义相关（同义表述）"),
        ("服务器设备一批 单价 2,000.00元 税率 13%", "语义无关（同为票据但内容不同）"),
        ("采购订单 订单号：PO-20260724-888 金额：55,000.00元", "语义无关（采购场景）"),
    ]

    qv = model.encode_one(query, is_query=True)
    dv = model.encode([d[0] for d in docs])

    sims = dv @ qv   # 双方均已归一化，点积即余弦相似度

    print(f"\n  查询: {query!r}\n")
    ranked = sorted(zip(docs, sims), key=lambda x: -x[1])
    for (text, label), s in ranked:
        bar = "█" * max(1, int(s * 40))
        print(f"  {s:+.4f} {bar:<30s} {label}")
        print(f"          {text[:56]}")

    print()
    print("=" * 74)
    print("批量编码性能")
    print("=" * 74)
    sample = [f"第{i}条测试文本 金额：{i*100}.00元 日期：2026-09-18" for i in range(32)]
    t0 = time.time()
    vecs = model.encode(sample, batch_size=8)
    cost = (time.time() - t0) * 1000
    print(f"  32 条文本编码耗时 {cost:.0f} ms（平均 {cost/32:.1f} ms/条）")
    print(f"  输出形状: {vecs.shape}  维度校验: {'通过' if vecs.shape[1] == model.dim else '失败'}")
    norms = np.linalg.norm(vecs, axis=1)
    print(f"  归一化校验: min={norms.min():.6f} max={norms.max():.6f} "
          f"{'通过' if abs(norms.min()-1) < 1e-4 else '失败'}")
