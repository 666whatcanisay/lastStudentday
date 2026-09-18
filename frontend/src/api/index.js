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
