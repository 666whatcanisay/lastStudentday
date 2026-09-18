/**
 * API 请求模块 — 封装所有后端接口调用
 */
import axios from 'axios'

// 创建 axios 实例，统一配置
const http = axios.create({
    baseURL: '/api',
    timeout: 30000,
})

// ==================== 响应拦截器 ====================
http.interceptors.response.use(
    (response) => {
        // 如果是 Blob 类型（如 Excel 导出），直接返回原始响应，不走 JSON 检查
        if (response.config.responseType === 'blob') {
            return response.data
        }
        const data = response.data
        if (data.code !== 200) {
            ElMessage.error(data.message || data.msg || '请求失败')
            return Promise.reject(data)
        }
        return data
    },
    (error) => {
        // 处理 FastAPI 422 校验错误，显示具体字段提示
        if (error.response?.status === 422) {
            const details = error.response?.data?.detail
            if (details && details.length > 0) {
                const msg = details.map(d => `${d.loc?.join('.') || ''}: ${d.msg}`).join('; ')
                ElMessage.error(`参数错误: ${msg}`)
            } else {
                ElMessage.error('请求参数校验失败 (422)')
            }
        } else {
            ElMessage.error(error.response?.data?.message || error.response?.data?.msg || error.message || '网络错误')
        }
        return Promise.reject(error)
    }
)

// 为拦截器引入 ElMessage（延迟加载避免循环依赖）
import { ElMessage } from 'element-plus'

// ==================== 接口定义 ====================

/** 文件上传 */
export function uploadFile(file, onProgress) {
    return http.post('/file/upload', file, {
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress: onProgress,
    })
}

/** OCR 识别 */
export function recognizeOCR(docId) {
    return http.post('/ocr/recognize', { doc_id: docId })
}

/** 文档统计数据 */
export function getDocumentStats() {
    return http.get('/document/stats')
}

/** 文档列表（分页+搜索） */
export function getDocumentList(params) {
    return http.get('/document/list', { params })
}

/** 文档详情 */
export function getDocumentDetail(docId) {
    return http.get(`/document/${docId}`)
}

/** 批量导出 Excel（返回 Blob） */
export function exportExcel(docIds) {
    return http.get('/document/export', {
        params: { doc_ids: docIds.join(',') },
        responseType: 'blob',
    })
}

/** 删除文档 */
export function deleteDocument(docId) {
    return http.delete(`/document/${docId}`)
}

/** 健康检查 / OCR 引擎信息 */
export function getHealth() {
    return http.get('/health')
}

// ==================== RAG 检索增强生成 ====================

/** RAG 配置与可用性（向量模型是否就绪、LLM 是否已配置） */
export function getRagConfig() {
    return http.get('/rag/config')
}

/** 索引状态（已索引/待索引/失败文档数、分块数） */
export function getRagIndexStatus() {
    return http.get('/rag/index/status')
}

/** 重建索引（不传 doc_ids 则全量） */
export function rebuildRagIndex(docIds) {
    return http.post('/rag/index', { doc_ids: docIds || null })
}

/** 重试未索引与索引失败的文档 */
export function retryRagIndex() {
    return http.post('/rag/index/retry')
}

/**
 * 纯检索（不调用 LLM）
 * 用于「检索模式对比」：同一查询分别用 vec / bm25 / hybrid 看效果差异
 */
export function ragSearch(params) {
    return http.post('/rag/search', {
        query: params.query,
        mode: params.mode || 'hybrid',
        top_k: params.topK ?? null,
        doc_ids: params.docIds ?? null,
        rewrite: params.rewrite !== false,
    })
}

/** 仅做查询改写（不检索），用于展示「系统把你的问题理解成了什么」 */
export function ragRewrite(query) {
    return http.post('/rag/rewrite', { query })
}

/**
 * 智能问答（完整链路：指代消解 → 查询改写 → 双路检索 → 融合 → LLM 生成 → 引用溯源）
 *
 * 单独设 60s 超时：这个接口要等云端 LLM 生成，
 * 全局的 30s 在长回答时可能不够。
 */
export function ragAsk(params) {
    return http.post(
        '/rag/ask',
        {
            question: params.question,
            mode: params.mode || 'hybrid',
            top_k: params.topK ?? null,
            doc_ids: params.docIds ?? null,
            rewrite: params.rewrite !== false,
            history: params.history ?? null,
        },
        { timeout: 60000 }
    )
}
