import { useEffect, useState } from 'react'
import { IconArrowUpRight } from '@tabler/icons-react'
import { focusTab } from 'moi'
import { getOverview } from './overview.server'

export const config = { colSpan: 4, rowSpan: 2 } as const

export default function Overview() {
  const [data, setData] = useState<any>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { let active = true; let loading = false
    const load = async () => { if (loading) return; loading = true; setBusy(true)
      try { const value = await getOverview(); if (active) { setData(value); setError('') } }
      catch { if (active) setError('暂时无法更新，当前保留上次内容') }
      finally { loading = false; if (active) setBusy(false) }
    }
    load(); const timer = setInterval(() => { if (!document.hidden) load() }, 30_000)
    return () => { active = false; clearInterval(timer) }
  }, [])
  const tasks = data?.tasks
  return <div className="flex h-full w-full flex-col gap-4 bg-background p-5 text-foreground">
    <div className="flex items-center justify-between gap-3 text-sm"><span>项目近况</span><span className="text-xs text-muted-foreground">{busy ? '更新中' : data ? new Date(data.captured_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '正在读取'}</span></div>
    <p className="text-2xl font-medium">{!data ? '正在了解项目…' : !tasks ? '任务状态暂时未知' : tasks.human.length ? `${tasks.human.length} 件事需要你决定` : tasks.blocked.length ? `${tasks.blocked.length} 项工作遇到阻碍` : tasks.active.length ? `正在推进 ${tasks.active.length} 项工作` : '现在可以看看下一步'}</p>
    <p className="line-clamp-2 text-sm leading-6 text-muted-foreground">{error || data?.errors[0]?.message || tasks?.active[0]?.display_title || tasks?.active[0]?.title || '查看最近成果，或随手做一次项目检查。'}</p>
    <button className="mt-auto flex min-h-11 items-center justify-between gap-2 rounded-lg bg-primary px-4 text-sm text-primary-foreground hover:bg-primary/90 focus-visible:ring-2 focus-visible:ring-ring" onClick={() => focusTab('view:home')}>打开项目首页<IconArrowUpRight size={18} aria-hidden /></button>
  </div>
}
