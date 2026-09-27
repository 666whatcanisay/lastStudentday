// A single domain event keeps tabs independent of one another's component refs.
export const DOCUMENTS_CHANGED = 'doc-ocr-documents-changed'

export function notifyDocumentsChanged() {
  window.dispatchEvent(new Event(DOCUMENTS_CHANGED))
}
