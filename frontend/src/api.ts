export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch('/api' + path, { ...init, headers: { ...(init.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...init.headers } });
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    const detail = typeof value.detail === 'string' ? value.detail : value.detail ? JSON.stringify(value.detail) : `请求失败 (${response.status})`;
    throw new Error(detail);
  }
  return response.status === 204 ? undefined as T : response.json();
}
export const post = <T = any>(path: string, data: unknown) => api<T>(path, { method: 'POST', body: JSON.stringify(data) });
export const put = <T = any>(path: string, data: unknown) => api<T>(path, { method: 'PUT', body: JSON.stringify(data) });
export const patch = <T = any>(path: string, data: unknown) => api<T>(path, { method: 'PATCH', body: JSON.stringify(data) });
export const items = <T = any>(v: T[] | {items: T[]}) => Array.isArray(v) ? v : v.items || [];
export const errorText = (e: unknown) => e instanceof Error ? e.message : '操作未完成，请重试。';
