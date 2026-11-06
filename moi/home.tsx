import { useCallback, useEffect, useRef, useState } from 'react'
import { IconArrowLeft, IconArrowRight, IconArrowUpRight, IconCheck, IconDeviceDesktop, IconRefresh, IconMessageCircle, IconPlugConnected, IconAlertTriangle } from '@tabler/icons-react'
import { fileUrl, sendChatMessage } from 'moi'
import { getOverview, getMachineUsage, inspect } from './home.server'

export const config = { title: '项目首页', icon: 'home' } as const
type Copy = { display_title?: string; display_summary?: string; explanation_stale?: boolean }
type Task = Copy & { id: string; title: string; description: string; close_reason: string; closed_at: string }
type Report = Copy & { path: string; title: string; date: string; summary: string }
type Overview = {
  captured_at: string; project: string; errors: { source: string; message: string }[]
  tasks: null | { active: Task[]; human: Task[]; blocked: Task[]; ready: Task[]; milestones: Task[]; open_count: number }
  reports: Report[]
}
type Detail = { title: string; summary?: string; body: string; source: string; ok?: boolean; entries?: string[]; captured_at?: string; assets?: { title: string; path: string }[] }
type Machine = {
  captured_at: string; version: string; scope: string; warnings: string[]
  projects: { name: string; path: string; current: boolean; state: string; error: string; task_error?: string; sync_version: string
    tasks: null | { open: number; active: number; human: number; blocked: number; updated_at: string } }[]
}
// Optional navigation from the overview widget. Omit to show the normal homepage.
type Params = { section?: string }
const button = 'inline-flex min-h-10 items-center justify-center gap-2 rounded-lg px-3 text-sm transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-wait disabled:opacity-50'
const time = (value: string) => new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
const short = (value: string, length = 160) => value.length > length ? value.slice(0, length) + '…' : value

export default function Home({ params = {} }: { params?: Params }) {
  const [data, setData] = useState<Overview | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [detail, setDetail] = useState<Detail | null>(null)
  const [actionError, setActionError] = useState('')
  const [target, setTarget] = useState('')
  const [clock, setClock] = useState(Date.now())
  const [sent, setSent] = useState(false)
  const loading = useRef(false)
  const alive = useRef(true)
  const detailRef = useRef<HTMLElement | null>(null)
  const [machine, setMachine] = useState<Machine | null>(null)
  const [machineError, setMachineError] = useState('')
  const [machineBusy, setMachineBusy] = useState(false)
  const machineLoading = useRef(false)
  const refreshMachine = useCallback(async () => {
    if (machineLoading.current) return
    machineLoading.current = true; setMachineBusy(true)
    try { const value = await getMachineUsage(); if (alive.current) { setMachine(value); setMachineError('') } }
    catch { if (alive.current) setMachineError('本机使用情况暂时无法更新。') }
    finally { machineLoading.current = false; if (alive.current) setMachineBusy(false) }
  }, [])
  const refresh = useCallback(async () => {
    if (loading.current) return
    void refreshMachine()
    loading.current = true
    setRefreshing(true)
    try {
      const value = await getOverview()
      if (alive.current) { setData(value); setError(''); setClock(Date.now()) }
    } catch { if (alive.current) setError('暂时无法更新。你可以重试，或让 Agent 检查连接。') }
    finally { loading.current = false; if (alive.current) setRefreshing(false) }
  }, [refreshMachine])
  useEffect(() => {
    alive.current = true
    refresh()
    const timer = setInterval(() => { setClock(Date.now()); if (!document.hidden) refresh() }, 30_000)
    const visible = () => { if (!document.hidden) refresh() }
    document.addEventListener('visibilitychange', visible)
    window.addEventListener('focus', visible)
    return () => { alive.current = false; clearInterval(timer); document.removeEventListener('visibilitychange', visible); window.removeEventListener('focus', visible) }
  }, [refresh])
  useEffect(() => {
    if (detail) detailRef.current?.focus({ preventScroll: true })
  }, [detail])
  useEffect(() => {
    if (params.section === 'actions') document.getElementById('quick-actions')?.scrollIntoView({ block: 'start' })
  }, [params.section])
  const open = async (kind: string, arg?: string) => {
    if (busy) return
    setBusy(kind); setActionError(''); setSent(false)
    try { setDetail(await inspect(kind, arg)); setTarget('') }
    catch (e) { setActionError(e instanceof Error ? e.message : '这次操作未能完成，请重试') }
    finally { setBusy('') }
  }
  const ask = (message: string, source?: string) => {
    try { sendChatMessage(message, { source, captured_at: data?.captured_at }); setSent(true) }
    catch { setActionError('未能发起对话，请打开聊天后重试') }
  }
  const tasks = data?.tasks
  const stale = !!data && (!!error || clock - Date.parse(data.captured_at) > 90_000)
  const taskRows = (rows: Task[]) => rows.slice(0, 4).map(task => (
    <button key={task.id} onClick={() => open('task', task.id)} disabled={!!busy} className="group flex min-h-12 w-full items-start gap-3 rounded-lg py-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
      <span className="min-w-0 flex-1 break-words text-sm leading-6 group-hover:underline">{task.display_title || task.title}{task.explanation_stale && <span className="block text-xs text-muted-foreground">记录已更新，通俗说明待更新</span>}</span>
      <IconArrowUpRight size={17} className="mt-1 shrink-0 text-muted-foreground" aria-hidden />
    </button>
  ))
  return (
    <main className="h-full w-full overflow-auto bg-background text-foreground">
      <div className="mx-auto max-w-6xl space-y-10 px-5 py-7 sm:px-9 sm:py-10">
        <header className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3 text-sm"><span className="font-medium">{data?.project || '项目'} / 工作台</span><span className="text-muted-foreground">项目简报</span></div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground"><span>{data ? `${stale ? '上次读取' : '更新于'} ${time(data.captured_at)}` : '正在读取项目情况'}</span><button className={button} onClick={refresh} disabled={refreshing} aria-label="刷新项目情况"><IconRefresh size={17} aria-hidden />{refreshing ? '更新中' : '刷新'}</button></div>
        </header>

        {(error || data?.errors.length) ? <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg bg-destructive/10 p-4 text-sm"><IconAlertTriangle size={19} aria-hidden /><div className="min-w-0 flex-1 break-words">{error || data?.errors.map(e => `${e.source}：${e.message}`).join('；')}{stale && ' 当前保留的是上次读取的内容。'}</div><button className={button} onClick={refresh} disabled={refreshing}>重试</button></div> : null}

        <section aria-labelledby="project-state" aria-busy={!data} className="space-y-4 border-b border-border pb-9">
          <p className="text-sm text-muted-foreground">现在怎样</p>
          <h1 id="project-state" className="break-words text-3xl font-medium leading-tight sm:text-4xl">{!data ? '正在了解项目…' : !tasks ? '项目状态暂时未知' : tasks.blocked.length ? `有 ${tasks.blocked.length} 项工作需要疏通。` : tasks.active.length ? `正在推进 ${tasks.active.length} 项工作。` : tasks.ready.length ? '可以开始下一项工作了。' : '当前没有进行中的工作。'}</h1>
          <p className="max-w-3xl break-words text-base leading-7 text-muted-foreground">{tasks ? tasks.active[0]?.display_summary || tasks.active[0]?.display_title || tasks.active[0]?.title || (tasks.ready.length ? '可开始的工作已列在下方。' : '可以从检查项目或回顾最近成果开始。') : data ? '任务来源读取成功后，这里会恢复显示。' : '情况来自项目的实际记录。'}</p>
          {tasks && <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm"><span>{tasks.open_count} 项未完成</span><span>{tasks.ready.length} 项可开始{tasks.ready.length === 5 ? '（最多显示 5 项）' : ''}</span><span>{tasks.human.length} 项需要你决定</span></div>}
        </section>

        <div className="grid gap-9 lg:grid-cols-2 lg:gap-16">
          <section aria-labelledby="current-work">
            <h2 id="current-work" className="mb-3 text-lg font-medium">正在做的事</h2>
            {tasks ? tasks.active.length ? taskRows(tasks.active) : <p className="py-3 text-sm leading-6 text-muted-foreground">暂时没有进行中的任务。</p> : <p className="py-3 text-sm text-muted-foreground">{data ? '任务来源暂不可用。' : '正在读取…'}</p>}
            {!!tasks?.blocked.length && <div className="mt-5"><h3 className="text-sm font-medium">遇到的阻碍</h3>{taskRows(tasks.blocked)}</div>}
            {!!tasks?.ready.length && !tasks.active.length && <div className="mt-5"><h3 className="text-sm font-medium">接下来可以做</h3>{taskRows(tasks.ready)}</div>}
          </section>
          <section aria-labelledby="your-decisions">
            <h2 id="your-decisions" className="mb-3 text-lg font-medium">需要你决定</h2>
            {tasks ? tasks.human.length ? taskRows(tasks.human) : <p className="py-3 text-sm leading-6 text-muted-foreground">暂时没有需要你决定的事项。<br />需要你判断时，Agent 会把问题和依据放到这里。</p> : <p className="py-3 text-sm text-muted-foreground">{data ? '待决定事项暂时无法确认。' : '正在读取…'}</p>}
          </section>
        </div>

        <section id="quick-actions" aria-labelledby="actions-title" className="space-y-4 border-y border-border py-7">
          <h2 id="actions-title" className="text-lg font-medium">随手做点什么</h2>
          <div className="flex flex-wrap gap-3">
            <button className={`${button} bg-primary text-primary-foreground hover:bg-primary/90`} onClick={() => open('check')} disabled={!!busy}><IconCheck size={18} aria-hidden />{busy === 'check' ? '正在检查…' : '检查项目'}</button>
            <button className={`${button} ring-1 ring-border`} onClick={() => open('local')} disabled={!!busy}><IconDeviceDesktop size={18} aria-hidden />{busy === 'local' ? '正在统计…' : '查看本机空间'}</button>
            <button className={`${button} ring-1 ring-border`} onClick={() => open('doctor')} disabled={!!busy}><IconPlugConnected size={18} aria-hidden />{busy === 'doctor' ? '正在核对…' : '检查连接'}</button>
          </div>
          <p className="text-xs leading-5 text-muted-foreground">检查连接会核对工具、账号与存储的可用性。清理空间先看预览，再决定下一步。</p>
          {actionError && <div role="alert" className="flex flex-wrap items-center gap-2 text-sm text-destructive"><IconAlertTriangle size={17} aria-hidden /><span className="break-words">{actionError}</span></div>}
          {!!busy && <p role="status" className="text-sm text-muted-foreground">正在读取结果，请稍候…</p>}
          {detail && <section ref={detailRef} tabIndex={-1} aria-label={detail.title} className="space-y-4 rounded-xl bg-card p-5 ring-1 ring-border focus:outline-none">
            <div className="flex items-start justify-between gap-3"><h3 className="break-words text-base font-medium">{detail.title}{detail.ok === false ? ' · 需要处理' : detail.ok === true ? ' · 已通过' : ''}</h3><button className={button} onClick={() => setDetail(null)} aria-label="收起结果"><IconArrowLeft size={16} aria-hidden />收起</button></div>
            <p className="break-words text-sm leading-7">{detail.summary || '这项记录暂无补充说明。'}</p>
            {!!detail.assets?.length && <div className="grid gap-4 sm:grid-cols-2">{detail.assets.map(asset => <a key={asset.path} href={fileUrl(asset.path)} target="_blank" rel="noreferrer" className="space-y-2 rounded-lg focus-visible:ring-2 focus-visible:ring-ring"><img src={fileUrl(asset.path)} alt={asset.title} loading="lazy" className="max-h-52 w-full rounded-lg object-contain ring-1 ring-border" /><span className="block text-xs text-muted-foreground">{asset.title} ↗</span></a>)}</div>}
            <details className="text-sm"><summary className="min-h-10 cursor-pointer py-2 text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring">查看原始依据</summary><div className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words text-sm leading-7">{detail.body || '这项记录暂无补充说明。'}</div></details>
            {detail.entries && <div className="space-y-3 border-t border-border pt-4"><p className="text-sm">{detail.entries.length ? '选择要查看清理预览的目录' : '当前没有可预览的运行目录。'}</p>{!!detail.entries.length && <div className="flex flex-wrap gap-3"><select aria-label="选择运行目录" className="min-h-10 min-w-0 max-w-full rounded-lg bg-background px-3 text-sm ring-1 ring-border focus-visible:ring-2 focus-visible:ring-ring" value={target} onChange={e => setTarget(e.target.value)}><option value="">请选择目录</option>{detail.entries.map(v => <option key={v} value={v}>{v}</option>)}</select><button className={`${button} ring-1 ring-border`} onClick={() => open('preview', target)} disabled={!target || !!busy}>预览清理</button></div>}</div>}
            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-3"><p className="min-w-0 break-all text-xs text-muted-foreground">依据：{detail.source}{detail.captured_at ? ` · ${time(detail.captured_at)}` : ''}</p><button className={button} disabled={sent || !!busy} onClick={() => ask(`请用我能理解的语言解释「${detail.title}」，说明结论、影响和建议，并核实当前来源。`, detail.source)}><IconMessageCircle size={17} aria-hidden />{sent ? '已请求解释' : '让 Agent 解释'}</button></div>
          </section>}
        </section>

        <section aria-labelledby="machine-title" aria-busy={machineBusy} className="space-y-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="space-y-2"><h2 id="machine-title" className="text-lg font-medium">本机 Keel 使用情况</h2><p className="text-sm text-muted-foreground">{machine ? `已安装 ${machine.version} · 找到 ${machine.projects.length} 个项目` : machineError ? '接入项目暂时无法确认' : '正在核对本机的接入项目…'}</p></div>
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"><span>{machine ? `${machineError ? '上次读取' : '读取于'} ${time(machine.captured_at)}` : ''}</span><button className={button} onClick={refreshMachine} disabled={machineBusy} aria-label="刷新本机使用情况"><IconRefresh size={16} aria-hidden />{machineBusy ? '更新中' : '刷新'}</button></div>
          </div>
          {machineError && <p role="alert" className="text-sm text-destructive">{machineError}{machine && ' 当前保留上次读取的内容。'} 请刷新重试。</p>}
          {!!machine?.warnings.length && <p role="status" className="text-sm leading-6 text-muted-foreground">{machine.warnings.join('；')}</p>}
          {machine ? <>
            <div className="divide-y divide-border">{machine.projects.map(project => <article key={project.path} className="grid gap-3 py-5 sm:grid-cols-[minmax(0,1fr)_minmax(0,2fr)] sm:gap-8">
              <div className="min-w-0 space-y-1"><h3 className="break-words text-base font-medium">{project.name}</h3><p className="text-xs text-muted-foreground">{project.current ? '当前项目 · ' : ''}{project.state === 'connected' ? '已接入' : project.state === 'unavailable' ? '目录或配置已失效' : '接入状态待确认'}</p></div>
              <div className="min-w-0 space-y-2">
                {project.tasks ? <><p className="text-sm">{project.tasks.active ? `${project.tasks.active} 项任务正在推进` : '没有进行中的任务'}</p><p className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground"><span>{project.tasks.open} 项未完成</span><span>{project.tasks.human} 项需要你决定</span>{project.tasks.blocked > 0 && <span>{project.tasks.blocked} 项已标为阻塞</span>}</p><p className="text-xs text-muted-foreground">{project.tasks.updated_at ? `任务最近更新 ${new Date(project.tasks.updated_at).toLocaleString('zh-CN', { year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}` : '暂无任务更新时间'}</p></> : <p className="text-sm text-muted-foreground">{project.task_error || project.error || '任务状态暂时未知'}</p>}
                {project.error && project.state === 'connected' && <p className="text-xs text-muted-foreground">{project.error}</p>}
                <details className="text-xs text-muted-foreground"><summary className="min-h-9 cursor-pointer py-2 focus-visible:ring-2 focus-visible:ring-ring">查看接入依据</summary><p className="break-all py-1 leading-6">{project.path}</p><p className="py-1">{project.sync_version ? `上次同步使用 Keel ${project.sync_version}` : '尚无可读取的同步版本记录'}</p></details>
              </div>
            </article>)}</div>
            {!machine.projects.length && <p className="text-sm text-muted-foreground">当前来源中还没有找到接入项目。</p>}
            <p className="text-xs leading-6 text-muted-foreground">{machine.scope} 进行中来自任务记录，不代表 Agent 或进程此刻在线。</p>
          </> : <p className="py-4 text-sm text-muted-foreground">{machineError ? '本机项目情况暂时未知。' : '读取接入情况和任务记录，不会修改其他项目。'}</p>}
        </section>

        <section aria-labelledby="results-title" className="space-y-6">
          <div className="flex flex-wrap items-end justify-between gap-2"><h2 id="results-title" className="text-lg font-medium">最近做成了什么</h2><p className="text-xs text-muted-foreground">从已完成的里程碑与实测报告回看</p></div>
          <div className="grid gap-9 lg:grid-cols-2 lg:gap-16">
            <div className="space-y-5"><h3 className="text-sm text-muted-foreground">已达成的能力</h3>{!tasks ? <p className="text-sm text-muted-foreground">{data ? '里程碑暂时无法读取。' : '正在读取…'}</p> : !tasks.milestones.length ? <p className="text-sm text-muted-foreground">还没有已完成的里程碑。</p> : tasks.milestones.map(item => <button key={item.id} disabled={!!busy} onClick={() => open('task', item.id)} className="group block w-full space-y-2 rounded-lg text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><p className="text-xs text-muted-foreground">{item.closed_at.slice(0, 10)}</p><p className="flex items-start gap-3 text-sm font-medium leading-6"><span className="min-w-0 flex-1 break-words group-hover:underline">{item.display_title || item.title}</span><IconArrowUpRight className="mt-1 shrink-0" size={16} aria-hidden /></p><p className="break-words text-sm leading-6 text-muted-foreground">{short(item.display_summary || item.close_reason || '查看这项里程碑的完成依据。')}{item.explanation_stale && '（记录已更新，通俗说明待更新）'}</p></button>)}</div>
            <div className="space-y-5"><h3 className="text-sm text-muted-foreground">实测成果</h3>{!data ? <p className="text-sm text-muted-foreground">正在读取…</p> : data.errors.some(e => e.source === '成果资料') ? <p className="text-sm text-muted-foreground">成果资料暂时无法读取。</p> : !data.reports.length ? <p className="text-sm text-muted-foreground">还没有已定稿的实测报告。</p> : data.reports.map(item => <button key={item.path} disabled={!!busy} onClick={() => open('note', item.path)} className="group block w-full space-y-2 rounded-lg text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><p className="text-xs text-muted-foreground">{item.date}</p><p className="flex items-start gap-3 text-sm font-medium leading-6"><span className="min-w-0 flex-1 break-words group-hover:underline">{item.display_title || item.title}</span><IconArrowUpRight className="mt-1 shrink-0" size={16} aria-hidden /></p><p className="break-words text-sm leading-6 text-muted-foreground">{short(item.display_summary || item.summary)}{item.explanation_stale && '（记录已更新，通俗说明待更新）'}</p></button>)}</div>
          </div>
        </section>
        <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-5 text-xs text-muted-foreground"><span>保持打开时，每 30 秒更新一次。</span><button className={button} disabled={sent || !data} onClick={() => ask('请结合项目的真实状态，帮我判断接下来最值得做什么，并说明理由。', '项目首页')}><IconMessageCircle size={17} aria-hidden />{sent ? '已请求分析' : '一起想想下一步'}<IconArrowRight size={16} aria-hidden /></button></footer>
      </div>
    </main>
  )
}
