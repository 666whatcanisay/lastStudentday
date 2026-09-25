/**
 * API 请求模块 — 封装所有后端接口调用
 */
import axios from 'axios'

// 创建 axios 实例，统一配置
const http = axios.create({
    baseURL: '/api',
    timeout: 30000,
})

/** 从 Content-Disposition 里取出后端给的中文文件名 */
function filenameFrom(disposition) {
    if (!disposition) return ''
    const m = /filename\*=UTF-8''([^;]+)/i.exec(disposition)
    if (m) {
        try { return decodeURIComponent(m[1]) } catch (e) { return m[1] }
    }
    const m2 = /filename="?([^";]+)"?/i.exec(disposition)
    return m2 ? m2[1] : ''
}

// ==================== 响应拦截器 ====================
http.interceptors.response.use(
    (response) => {
        // Blob 类响应（PDF/ZIP/Excel 导出）：
        // 必须先看 Content-Type —— 后端出错时返回的是 **JSON**，
        // 若当成文件交给用户保存，就会得到一个名为 xxx.pdf 的错误文本，
        // 用户打开只看到一串 JSON，完全不知道发生了什么。
        if (response.config.responseType === 'blob') {
            const ct = String(response.headers?.['content-type'] || '')
            if (ct.includes('application/json')) {
                return new Promise((resolve, reject) => {
                    const reader = new FileReader()
                    reader.onerror = () => {
                        ElMessage.error('导出失败：无法读取服务端响应')
                        reject(new Error('export-response-unreadable'))
                    }
                    reader.onload = () => {
                        try {
                            const d = JSON.parse(String(reader.result))
                            ElMessage.error(d.message || d.msg || '导出失败')
                            reject(d)
                        } catch (e) {
                            ElMessage.error('导出失败')
                            reject(new Error('export-failed'))
                        }
                    }
                    reader.readAsText(response.data)
                })
            }
            return {
                data: response.data,
                filename: filenameFrom(response.headers?.['content-disposition']),
            }
        }
        const data = response.data
        if (data.code !== 200) {
            ElMessage.error(data.message || data.msg || '请求失败')
            return Promise.reject(data)
        }
        return data
    },
    (error) => {
        if (error.response?.status === 401) {
            window.dispatchEvent(new Event('doc-ocr-auth-expired'))
        }
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
            // 优先用后端给的 msg（后端返回的都是中文、可操作）。
            // 没有 response 时（超时 / 断网）axios 的 error.message 是**英文**的
            // （"timeout of 30000ms exceeded" / "Network Error"），
            // 直接透给用户等于没有提示，所以这里必须映射成中文。
            const backendMsg = error.response?.data?.message || error.response?.data?.msg
            let fallback
            if (error.code === 'ECONNABORTED' || /timeout/i.test(error.message || '')) {
                fallback = '请求超时：文档较大或服务繁忙，请稍后重试'
            } else if (!error.response) {
                fallback = '网络错误：无法连接后端服务，请确认服务已启动'
            } else {
                fallback = `请求失败（HTTP ${error.response.status}）`
            }
            ElMessage.error(backendMsg || fallback)
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

/**
 * OCR 识别
 *
 * 单独设 300s 超时：多页 PDF 是**逐页**推理的。实测（本机，已含图像预处理提速）：
 *   5 页 ≈ 21s，10 页 ≈ 35s
 * 全局的 30s 对 10 页以上必然误报超时 —— 而服务端其实还在正常跑完，
 * 用户看到「识别失败」后重试，反而造成重复识别。
 */
export function recognizeOCR(docId) {
    return http.post('/ocr/recognize', { doc_id: docId }, { timeout: 300000 })
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

/**
 * 批量导出 Excel（返回 { data: Blob, filename }）
 *
 * 拦截器已处理「后端返回 JSON 错误」的情况：那种请求会被 reject，
 * 不会把错误文本当成 xlsx 存下来。
 */
export function exportExcel(docIds) {
    return http.get('/document/export', {
        params: { doc_ids: docIds.join(',') },
        responseType: 'blob',
        timeout: 180000,
    })
}

/**
 * 单份 PDF 识别结果报告
 * attachOriginal=true 时若原件缺失，后端明确报错（不静默省略附录）
 */
export function exportReport(docId, attachOriginal = false) {
    return http.get('/document/export/report', {
        params: { doc_id: docId, attach_original: attachOriginal },
        responseType: 'blob',
        timeout: 180000,
    })
}

/**
 * 批量 PDF 报告 ZIP（最多 20 份；部分成功可下载，全部失败后端直接报错）
 * 超时给到 5 分钟：附原件时要把原件逐份写进 ZIP，20 份可能不快。
 */
export function exportReports(docIds, attachOriginal = false) {
    return http.get('/document/export/reports', {
        params: { doc_ids: docIds.join(','), attach_original: attachOriginal },
        responseType: 'blob',
        timeout: 300000,
    })
}

/** 把 Blob 存成文件（用后端给的文件名，避免中文名乱码） */
export function saveBlob(blob, filename) {
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename || 'export'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    // 立刻 revoke 在部分浏览器会取消下载，延后释放更稳
    setTimeout(() => URL.revokeObjectURL(url), 3000)
}

/** 删除文档（软删除，进回收站） */
export function deleteDocument(docId) {
    return http.delete(`/document/${docId}`)
}

/** 从回收站恢复 */
export function restoreDocument(docId) {
    return http.post(`/document/${docId}/restore`)
}

/** 永久删除（仅回收站条目） */
export function permanentDeleteDocument(docId) {
    return http.delete(`/document/${docId}/permanent`)
}

/**
 * 编辑文档（乐观并发）
 *
 * 后端 expected_revision 与当前版本不一致时返回 code=409（HTTP 200 包体里的 code 字段），
 * 拦截器会把非 200 的包体整体 reject，调用方拿 e.code === 409 判断冲突。
 */
export function patchDocument(docId, payload) {
    return http.patch(`/document/${docId}`, payload)
}

/** 版本历史摘要 */
export function getDocumentRevisions(docId) {
    return http.get(`/document/${docId}/revisions`)
}

/** 只读版本快照 */
export function getDocumentRevision(docId, revision) {
    return http.get(`/document/${docId}/revisions/${revision}`)
}

/**
 * 字段规格（某类型的稳定字段键与中文标签）
 * 用于切换类型时在界面上列出新字段，**保存前**让用户编辑，而不是悄悄提交。
 */
export function getFieldSpecs(docType) {
    return http.get('/document/field-specs', { params: { doc_type: docType } })
}

/**
 * 重新识别（force=true）
 * 已有的识别结果不会被悄悄覆盖：后端要求已有有效版本时必须显式 force，
 * 前端在此之前弹确认框，用户点确认才带这个参数。
 */
export function reRecognizeOCR(docId) {
    return http.post('/ocr/recognize', { doc_id: docId, force: true }, { timeout: 300000 })
}

/**
 * 原件预览/下载 URL
 * 只传文档 ID，不接受客户端文件路径（后端按 ID 查库并校验路径在存储目录内）
 */
export function previewUrl(docId, download = false) {
    return `/api/file/preview/${docId}${download ? '?download=true' : ''}`
}

/**
 * 分类统计（任务书 3.4 唯一口径）
 *
 * 返回的金额一律是**十进制字符串**，前端只做展示，绝不重新计算财务合计 ——
 * 一旦前端自己加一遍，就会和后端口径不一致（跨币种混算就是从这种地方长出来的）。
 */
export function getAnalytics(params) {
    return http.get('/document/analytics', { params })
}

/**
 * 统计明细穿透：与图表共用同一套过滤条件
 * 额外支持 month / party_role+party / require_amount（金额图点开时必须带）
 */
export function getAnalyticsDetails(params) {
    return http.get('/document/analytics/details', { params })
}

// ==================== 数据维护（备份 / 恢复说明） ====================

/**
 * 手动一致性备份（返回 { data: Blob, filename }）
 * 后端在写入门禁等待超时（60s）时返回 409 + 明确提示，前端不要反复重试同一个任务。
 */
export function createBackup() {
    return http.post('/maintenance/backup', null, {
        responseType: 'blob',
        timeout: 300000,
    })
}

/** 恢复说明与脚本入口（只给项目内相对路径） */
export function getRestoreInfo() {
    return http.get('/maintenance/restore-info')
}

/** 健康检查 / OCR 引擎信息 */
export function getHealth() {
    return http.get('/health')
}

// ==================== 临时公网演示登录 ====================

export function getAuthStatus() {
    return http.get('/auth/me')
}

export function loginDemo(password) {
    return http.post('/auth/login', { password })
}

export function logoutDemo() {
    return http.post('/auth/logout')
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
