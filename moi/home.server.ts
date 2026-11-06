import { bridge } from '../lib/bridge'
export async function getOverview() { return bridge('overview') }
export async function getMachineUsage() { return bridge('machine') }
export async function inspect(kind: string, arg?: string) {
  if (!['task', 'note', 'check', 'local', 'preview', 'doctor'].includes(kind)) throw new Error('不支持此操作')
  return bridge(kind, arg)
}
