import { realpath, readFile } from 'node:fs/promises'
import { spawn } from 'node:child_process'

// All public functions go through the same bounded adapter; no shell or arbitrary command RPC.
export async function bridge(action: string, arg?: string) {
  const executable = Bun.which('keel')
  if (!executable) throw new Error('本机还没有安装 Keel，请让 Agent 检查环境')
  const header = (await readFile(await realpath(executable), 'utf8')).split('\n')[0]
  const interpreter = header.startsWith('#!/') ? header.slice(2).trim() : ''
  if (!interpreter || interpreter.includes(' ')) throw new Error('无法识别 Keel 的运行环境，请让 Agent 检查')
  return new Promise<any>((resolve, reject) => {
    const child = spawn(interpreter, ['work/moi/bridge.py'], { cwd: process.cwd(), stdio: ['pipe', 'pipe', 'pipe'] })
    let output = '', size = 0
    const timer = setTimeout(() => { child.kill('SIGKILL'); reject(new Error('操作超时，请稍后重试')) }, 50_000)
    child.stdout.on('data', (part: Buffer) => {
      size += part.length
      if (size > 2_000_000) { child.kill('SIGKILL'); return }
      output += part.toString()
    })
    // Tool diagnostics are returned deliberately by the adapter, never leaked from process stderr.
    child.stderr.resume()
    child.on('error', () => { clearTimeout(timer); reject(new Error('无法启动本机工具，请让 Agent 检查')) })
    child.on('close', () => {
      clearTimeout(timer)
      try {
        const value = JSON.parse(output)
        if (!value.ok) reject(new Error(value.error))
        else resolve(value.data)
      } catch { reject(new Error('工具未返回有效结果，请重试')) }
    })
    child.stdin.on('error', () => {})
    child.stdin.end(JSON.stringify({ action, arg }))
  })
}
