import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { WEBAPP_LAUNCH_REQUIRED } from '@/lib/browser-launch-session'
import { $desktopBoot } from '@/store/boot'
import { $connection } from '@/store/session'
import { clearWebappSession, seedWebappSession } from '@/test/webapp-session'

import { installBrowserDesktopBridge } from './browser-desktop-bridge'

const META = '\u0000HERMES_TERMINAL_META:'

class Socket {
  static OPEN = 1
  static instances: Socket[] = []
  static reply = (socket: Socket) => socket.metadata()
  static acceptStartupCancellation = true
  readyState = 1
  binaryType = ''
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: (() => void) | null = null
  sent: string[] = []
  url: URL

  constructor(url: string | URL) {
    this.url = new URL(url)
    Socket.instances.push(this)
    queueMicrotask(() => {
      if (Socket.acceptStartupCancellation && this.url.searchParams.get('startup') === '1') {
        this.onmessage?.({ data: META + '{"preparing":true,"ackStartup":true}' } as MessageEvent)
      }
      Socket.reply(this)
    })
  }

  metadata(extra: Record<string, unknown> = {}) {
    this.onmessage?.({ data: META + JSON.stringify({
      shell: 'bash', cwd: '/original', terminalId: 'opaque-server-id', retentionSeconds: 900,
      reconnected: this.url.searchParams.has('attach'), ...extra
    }) } as MessageEvent)
  }

  output(text: string) {
    this.onmessage?.({ data: new TextEncoder().encode(text).buffer } as MessageEvent)
  }

  close(code = 1000, reason = '') {
    this.readyState = 3
    this.onclose?.(new CloseEvent('close', { code, reason }))
  }

  send(data: string) { this.sent.push(data) }
}

function install() {
  Reflect.deleteProperty(window, 'hermesDesktop')
  Object.assign(window, { __HERMES_AUTH_REQUIRED__: true, __HERMES_BASE_PATH__: '/one' })
  installBrowserDesktopBridge()

  return window.hermesDesktop!.terminal
}

beforeEach(() => {
  localStorage.clear()
  vi.useFakeTimers()
  Socket.instances = []
  Socket.acceptStartupCancellation = true
  Socket.reply = socket => socket.metadata()
  vi.stubGlobal('WebSocket', Socket)
  let tickets = 0
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ticket: `fresh-${++tickets}` }))))
  $connection.set({ profile: 'original' } as never)
})

afterEach(() => {
  window.dispatchEvent(new Event('beforeunload'))
  Reflect.deleteProperty(window, 'hermesDesktop')
  Reflect.deleteProperty(window, '__HERMES_AUTH_REQUIRED__')
  Reflect.deleteProperty(window, '__HERMES_BASE_PATH__')
  Reflect.deleteProperty(window, '__HERMES_TERMINAL_ENVIRONMENT__')
  $connection.set(null)
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('persistent browser terminals', () => {
  it.each(['executor', null] as const)('skips independent cwd resolution for a local terminal in %s mode', async mode => {
    window.__HERMES_TERMINAL_ENVIRONMENT__ = mode
    const api = install()
    await api.start()
    expect(Socket.instances[0].url.searchParams.has('cwd')).toBe(false)
    expect(Socket.instances[0].url.searchParams.get('environment')).toBe(mode ?? 'unavailable')
    expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes('/api/fs/default-cwd'))).toBe(false)

    await api.start({ cwd: '/explicit path ' })
    expect(Socket.instances[1].url.searchParams.get('cwd')).toBe('/explicit path ')
  })

  it('consumes an empty replay frame without suppressing the first live output', async () => {
    const api = install()
    const session = await api.start({ cwd: '/original', restoreKey: 'empty-history' })
    const output = vi.fn()
    api.onData(session.id, output)
    Socket.instances[0].output('')
    Socket.instances[0].output('\u001b[6n')
    expect(output).toHaveBeenCalledExactlyOnceWith('\u001b[6n')
  })

  it('reattaches the saved tab after reload to its captured profile, not the newly active one', async () => {
    const firstApi = install()
    await firstApi.start({ cwd: '/original', restoreKey: 'tab-one' })
    window.dispatchEvent(new Event('beforeunload'))
    $connection.set({ profile: 'other' } as never)
    const secondApi = install()
    const session = await secondApi.start({ cwd: '/other', restoreKey: 'tab-one' })
    const socket = Socket.instances.at(-1)!
    expect(socket.url.searchParams.get('attach')).toBe('opaque-server-id')
    expect(socket.url.searchParams.get('profile')).toBe('original')
    expect(session.cwd).toBe('/original')
    expect(socket.url.searchParams.get('ticket')).not.toBe(Socket.instances[0].url.searchParams.get('ticket'))
    expect(Socket.instances.some(s => s.url.searchParams.get('action') === 'close')).toBe(false)
  })

  it('automatically reconnects without reporting exit, resets before replay, and does not send input offline', async () => {
    const api = install()
    const session = await api.start({ cwd: '/original', restoreKey: 'tab-one' })
    const output = vi.fn()
    const exit = vi.fn()
    api.onData(session.id, output)
    api.onExit(session.id, exit)
    Socket.instances[0].output('before')
    Socket.instances[0].close(1006)
    await expect(api.write(session.id, 'dangerous command\r')).resolves.toBe(false)
    await api.resize(session.id, { cols: 97, rows: 31 })
    await vi.advanceTimersByTimeAsync(1000)
    expect(Socket.instances).toHaveLength(2)
    const resumed = Socket.instances[1]
    expect(resumed.url.searchParams.get('attach')).toBe('opaque-server-id')
    expect(resumed.url.searchParams.get('cols')).toBe('97')
    expect(resumed.sent).toContain('\u001b[RESIZE:97;31]')
    resumed.output('before\r\nafter')
    const frames = output.mock.calls.map(([data]) => data as string)
    expect(frames.at(-2)).toContain('\u001bc')
    expect(frames.at(-1)).toBe('before\r\nafter')
    expect(exit).not.toHaveBeenCalled()
    expect(resumed.sent.join('')).not.toContain('dangerous command')
    expect(resumed.sent.join('')).not.toContain('\u000c')
  })

  // A trimmed replay can start mid-way through a full-screen program; an unchanged size sends no SIGWINCH.
  it('forces one redraw by a real size change only when resuming from a trimmed replay', async () => {
    const api = install()
    await api.start({ cwd: '/original', restoreKey: 'tab-one' })
    Socket.instances[0].close(1006)
    await vi.advanceTimersByTimeAsync(1000)
    expect(Socket.instances[1].sent).toEqual(['\u001b[RESIZE:80;24]'])
    Socket.reply = socket => socket.metadata({ truncated: true })
    Socket.instances[1].close(1006)
    await vi.advanceTimersByTimeAsync(1000)
    expect(Socket.instances[2].sent).toEqual(['\u001b[RESIZE:80;23]', '\u001b[RESIZE:80;24]'])
  })

  it('closes explicitly with a fresh authenticated request even while disconnected', async () => {
    const api = install()
    const session = await api.start({ cwd: '/original', restoreKey: 'tab-one' })
    Socket.instances[0].close(1006)

    Socket.reply = socket => {
      socket.metadata({ closed: true })
      socket.close()
    }

    await expect(api.dispose(session.id)).resolves.toBe(true)
    const close = Socket.instances.at(-1)!
    expect(close.url.searchParams.get('action')).toBe('close')
    expect(close.url.searchParams.get('attach')).toBe('opaque-server-id')
    expect(close.url.searchParams.get('profile')).toBe('original')
    await vi.advanceTimersByTimeAsync(30_000)
    expect(Socket.instances).toHaveLength(2)
    expect(localStorage.length).toBe(0)
  })

  it('stops on supersession rather than fighting the other tab, and retains the resume identity', async () => {
    const api = install()
    const session = await api.start({ cwd: '/original', restoreKey: 'tab-one' })
    const exit = vi.fn()
    api.onExit(session.id, exit)
    Socket.instances[0].close(4409)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(Socket.instances).toHaveLength(1)
    expect(exit).toHaveBeenCalledWith({ code: null, signal: 'superseded' })
    expect(localStorage.length).toBe(1)
  })

  it('reports expired resumes without silently creating a replacement shell', async () => {
    const api = install()
    const session = await api.start({ cwd: '/original', restoreKey: 'tab-one' })
    const exit = vi.fn()
    api.onExit(session.id, exit)
    Socket.instances[0].close(1006)
    Socket.reply = socket => socket.close(4410, 'Terminal expired')
    await vi.advanceTimersByTimeAsync(30_000)
    expect(Socket.instances).toHaveLength(2)
    expect(exit).toHaveBeenCalledWith({ code: null, signal: 'expired' })
    expect(localStorage.length).toBe(0)
  })

  // The server closes with 4410 both when the shell exits (`exit`, Ctrl-D) and when it ends the
  // shell itself (expiry, a deleted profile, shutdown); only an exit carries the reason.
  it('reports a host shell that exited as an exit, as native Desktop does, and forgets it', async () => {
    const api = install()
    const session = await api.start({ cwd: '/original', restoreKey: 'tab-one' })
    const exit = vi.fn()
    api.onExit(session.id, exit)
    Socket.instances[0].close(4410, 'Process exited')
    await vi.advanceTimersByTimeAsync(30_000)
    expect(Socket.instances).toHaveLength(1)
    expect(exit).toHaveBeenCalledExactlyOnceWith({ code: null, signal: null })
    expect(localStorage.length).toBe(0)
  })

  // The shell exited while the tab was away: the redial is refused with the exit reason.
  it('reports a redial refused because the shell exited as an exit, and forgets it', async () => {
    const api = install()
    const session = await api.start({ cwd: '/original', restoreKey: 'tab-one' })
    const exit = vi.fn()
    api.onExit(session.id, exit)
    Socket.instances[0].close(1006)
    Socket.reply = socket => socket.close(4410, 'Process exited')
    await vi.advanceTimersByTimeAsync(30_000)
    expect(Socket.instances).toHaveLength(2)
    expect(exit).toHaveBeenCalledExactlyOnceWith({ code: null, signal: null })
    expect(localStorage.length).toBe(0)
  })

  it('rejects resuming a saved shell that exited with the exit signal, and forgets it', async () => {
    const api = install()
    await api.start({ cwd: '/original', restoreKey: 'tab-one' })
    Socket.reply = socket => socket.close(4410, 'Process exited')
    await expect(api.start({ restoreKey: 'tab-one', resumeOnly: true })).rejects.toMatchObject({ signal: 'exited' })
    expect(Socket.instances[1].url.searchParams.get('attach')).toBe('opaque-server-id')
    expect(Socket.instances[1].url.searchParams.has('action')).toBe(false)
    expect(localStorage.length).toBe(0)
  })

  it('reports a 4410 close without the exit reason as expired', async () => {
    const api = install()
    const session = await api.start({ cwd: '/original', restoreKey: 'tab-one' })
    const exit = vi.fn()
    api.onExit(session.id, exit)
    Socket.instances[0].close(4410)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(Socket.instances).toHaveLength(1)
    expect(exit).toHaveBeenCalledExactlyOnceWith({ code: null, signal: 'expired' })
  })

  it('never retries denied attachments or silently adopts the active profile', async () => {
    const api = install()
    const session = await api.start({ cwd: '/original', restoreKey: 'tab-one' })
    Socket.instances[0].close(1006)
    const exit = vi.fn()
    api.onExit(session.id, exit)
    $connection.set({ profile: 'other' } as never)
    Socket.reply = socket => socket.close(4403)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(Socket.instances).toHaveLength(2)
    expect(exit).toHaveBeenCalledWith({ code: null, signal: 'denied' })
    expect(Socket.instances[1].url.searchParams.get('profile')).toBe('original')
  })

  it('bounds network retries and resumes after bfcache without duplicating viewers', async () => {
    const api = install()
    const session = await api.start({ cwd: '/original', restoreKey: 'tab-one' })
    const state = vi.fn()
    const exit = vi.fn()
    api.onState!(session.id, state)
    api.onExit(session.id, exit)
    window.dispatchEvent(new Event('beforeunload'))
    window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }))
    await vi.advanceTimersByTimeAsync(30_000)
    expect(Socket.instances).toHaveLength(1)
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))
    await vi.advanceTimersByTimeAsync(1000)
    expect(Socket.instances).toHaveLength(2)
    expect(Socket.instances[1].url.searchParams.get('attach')).toBe('opaque-server-id')
    Socket.reply = socket => socket.close(1006)
    Socket.instances[1].close(1006)
    await vi.advanceTimersByTimeAsync(120_000)
    expect(state).toHaveBeenCalledWith('reconnecting')
    expect(state).toHaveBeenLastCalledWith('disconnected')
    expect(exit).toHaveBeenCalledWith({ code: null, signal: 'disconnected' })
    const attempts = Socket.instances.length
    expect(attempts).toBeLessThanOrEqual(7)
    await vi.advanceTimersByTimeAsync(120_000)
    expect(Socket.instances).toHaveLength(attempts)
    expect(Socket.instances.slice(1).every(socket => socket.url.searchParams.has('attach'))).toBe(true)
  })

  it('waits for close acknowledgement and preserves the handle on a denied close', async () => {
    const api = install()
    await api.start({ cwd: '/original', restoreKey: 'tab-one' })
    Socket.reply = socket => socket.close(4403)
    await expect(api.closeSaved!('tab-one')).rejects.toThrow(/close/i)
    expect(localStorage.length).toBe(1)

    Socket.reply = socket => { socket.metadata({ closed: true }); socket.close() }
    await expect(api.closeSaved!('tab-one')).resolves.toBe(true)
    expect(localStorage.length).toBe(0)
  })

  it('does not redial a late auth ticket after detach', async () => {
    const api = install()
    const session = await api.start({ cwd: '/original', restoreKey: 'tab-one' })
    let finishTicket!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { finishTicket = resolve })))
    Socket.instances[0].close(1006)
    await vi.advanceTimersByTimeAsync(1000)
    await api.detach!(session.id)
    finishTicket(new Response(JSON.stringify({ ticket: 'late' })))
    await vi.advanceTimersByTimeAsync(30_000)
    expect(Socket.instances).toHaveLength(1)
  })

  it('does not share persisted terminals between installations behind different proxy prefixes', async () => {
    await install().start({ cwd: '/original', restoreKey: 'tab-one' })
    window.dispatchEvent(new Event('beforeunload'))
    Reflect.deleteProperty(window, 'hermesDesktop')
    Object.assign(window, { __HERMES_BASE_PATH__: '/two' })
    installBrowserDesktopBridge()
    await window.hermesDesktop!.terminal.start({ cwd: '/original', restoreKey: 'tab-one' })
    expect(Socket.instances.at(-1)!.url.searchParams.has('attach')).toBe(false)
  })
})

describe('host terminal after sign-out', () => {
  /** A Webapp tab on the loopback bind, signed in by its launch link. */
  function installWebapp() {
    Reflect.deleteProperty(window, 'hermesDesktop')
    Object.assign(window, { __HERMES_AUTH_REQUIRED__: false, __HERMES_BASE_PATH__: '/one' })
    seedWebappSession('launch-link-session')
    installBrowserDesktopBridge()

    return window.hermesDesktop!.terminal
  }

  const signedOutTicket = () =>
    vi.fn(async () => new Response(JSON.stringify({ detail: 'Unauthorized' }), { status: 401 }))

  afterEach(() => {
    clearWebappSession()
    $desktopBoot.set({ ...$desktopBoot.get(), error: null, visible: false })
  })

  // `hermes webapp --sign-out` closes the tab's sockets with 4401. That is final: redialing only
  // hits a ticket route that answers 401, so the terminal stops at once and the tab shows the
  // sign-in view the chat shows.
  it('stops on a 4401 close without redialing and shows the Webapp sign-in view', async () => {
    const api = installWebapp()
    const session = await api.start({ cwd: '/original', restoreKey: 'tab-one' })
    const exit = vi.fn()
    api.onExit(session.id, exit)
    const ticketsBefore = vi.mocked(fetch).mock.calls.length

    Socket.instances[0].close(4401, 'signed out')
    await vi.advanceTimersByTimeAsync(60_000)

    expect(Socket.instances).toHaveLength(1)
    expect(fetch).toHaveBeenCalledTimes(ticketsBefore)
    expect(exit).toHaveBeenCalledExactlyOnceWith({ code: null, signal: 'signed-out' })
    expect($desktopBoot.get().error).toBe(WEBAPP_LAUNCH_REQUIRED)
  })

  it('stops reconnecting once the ticket route answers 401', async () => {
    const api = installWebapp()
    const session = await api.start({ cwd: '/original', restoreKey: 'tab-one' })
    const exit = vi.fn()
    api.onExit(session.id, exit)
    const ticket = signedOutTicket()
    vi.stubGlobal('fetch', ticket)

    Socket.instances[0].close(1006)
    await vi.advanceTimersByTimeAsync(120_000)

    expect(ticket).toHaveBeenCalledTimes(1)
    expect(Socket.instances).toHaveLength(1)
    expect(exit).toHaveBeenCalledExactlyOnceWith({ code: null, signal: 'signed-out' })
    expect($desktopBoot.get().error).toBe(WEBAPP_LAUNCH_REQUIRED)
  })

  it('shows the sign-in view when a new terminal cannot get a ticket', async () => {
    const api = installWebapp()
    vi.stubGlobal('fetch', signedOutTicket())

    await expect(api.start({ cwd: '/original', restoreKey: 'tab-two' })).rejects.toThrow(WEBAPP_LAUNCH_REQUIRED)

    expect(Socket.instances).toHaveLength(0)
    expect($desktopBoot.get().error).toBe(WEBAPP_LAUNCH_REQUIRED)
  })

  // Behind the sign-in gate the chat's next ticket request takes the tab to the login page; the
  // Webapp's launch-link view would name the wrong way to sign in, so the terminal only stops.
  it('stops on a 4401 close behind the sign-in gate without the launch-link view', async () => {
    const api = install()
    const session = await api.start({ cwd: '/original', restoreKey: 'tab-one' })
    const exit = vi.fn()
    api.onExit(session.id, exit)

    Socket.instances[0].close(4401, 'signed out')
    await vi.advanceTimersByTimeAsync(60_000)

    expect(Socket.instances).toHaveLength(1)
    expect(exit).toHaveBeenCalledExactlyOnceWith({ code: null, signal: 'signed-out' })
    expect($desktopBoot.get().error).not.toBe(WEBAPP_LAUNCH_REQUIRED)
  })
})

describe('executor startup cancellation', () => {
  afterEach(() => { Reflect.deleteProperty(window, '__HERMES_TERMINAL_ENVIRONMENT__') })

  it.each(['cwd', 'url', 'metadata'] as const)('closes during pending %s without a late socket or metadata revival', async stage => {
    const { createBrowserTerminal } = await import('./browser-terminal')
    window.__HERMES_TERMINAL_ENVIRONMENT__ = 'executor'
    let release!: (value: string) => void
    const pending = new Promise<string>(resolve => { release = resolve })
    Socket.reply = () => undefined
    const api = createBrowserTerminal({ basePath: '', currentProfile: () => null, currentConnection: () => 'local',
      defaultCwd: () => stage === 'cwd' ? pending : Promise.resolve('/work'),
      websocketUrl: () => stage === 'url' ? pending : Promise.resolve('ws://localhost/terminal') })
    const start = api.start({ restoreKey: 'pending' })
    const failed = expect(start).rejects.toThrow('cancelled')
    if (stage === 'metadata') {await vi.waitFor(() => expect(Socket.instances).toHaveLength(1))}
    expect(await api.closeSaved!('pending')).toBe(true)
    release(stage === 'url' ? 'ws://localhost/late' : '/late')
    await failed
    await Promise.resolve()
    for (const socket of Socket.instances) {
      socket.metadata()
      expect(socket.readyState).toBe(3)
    }
    expect(Socket.instances).toHaveLength(stage === 'metadata' ? 1 : 0)
  })

  it('cancels overlapping remount starts without spawning a successor', async () => {
    Socket.reply = () => undefined
    window.__HERMES_TERMINAL_ENVIRONMENT__ = 'executor'
    const api = install()
    const first = api.start({ cwd: '/work', restoreKey: 'remount' })
    const firstFailed = expect(first).rejects.toThrow('cancelled')
    const second = api.start({ cwd: '/work', restoreKey: 'remount' })
    const secondFailed = expect(second).rejects.toThrow('cancelled')
    await vi.waitFor(() => expect(Socket.instances).toHaveLength(1))
    expect(await api.closeSaved!('remount')).toBe(true)
    await Promise.all([firstFailed, secondFailed])
    expect(Socket.instances).toHaveLength(1)
    expect(Socket.instances[0].readyState).toBe(3)
  })

  it('allows slow fresh executor preparation and bounds a stalled reattach', async () => {
    Socket.reply = () => undefined
    window.__HERMES_TERMINAL_ENVIRONMENT__ = 'executor'
    const api = install()
    const started = vi.fn()
    const starting = api.start({ cwd: '/work', restoreKey: 'executor-startup' }).then(result => { started(); return result })
    await vi.waitFor(() => expect(Socket.instances).toHaveLength(1))
    await vi.advanceTimersByTimeAsync(60_000)
    expect(started).not.toHaveBeenCalled()
    Socket.instances[0].metadata()
    expect((await starting).cwd).toBe('/original')
    Socket.instances[0].close(1006)
    await vi.waitFor(() => expect(Socket.instances).toHaveLength(2))
    expect(Socket.instances[1].url.searchParams.get('attach')).toBe('opaque-server-id')
    await vi.advanceTimersByTimeAsync(10_000)
    expect(Socket.instances[1].readyState).toBe(3)
  })
})

it('keeps the tab owner through slow relayed startup and closes a published shell by its token', async () => {
  const { createBrowserTerminal } = await import('./browser-terminal')
  Socket.reply = socket => {
    if (socket.url.searchParams.get('action') === 'close') {
      socket.metadata({ terminalId: socket.url.searchParams.get('attach'), closed: true })
      socket.close()
    }
  }
  const websocketUrl = vi.fn(async () => 'ws://localhost/gateway-relay/tab-owner/api/host-terminal')
  const defaultCwd = vi.fn(async () => '/remote')
  const api = createBrowserTerminal({ basePath: '', currentProfile: () => 'selected-profile',
    currentConnection: () => 'another-gateway', defaultCwd, websocketUrl })
  const starting = api.start({ restoreKey: 'remote-pending', connectionId: 'tab-owner', profile: 'tab-profile' })
  await vi.waitFor(() => expect(Socket.instances).toHaveLength(1))
  expect(defaultCwd).toHaveBeenCalledWith('tab-profile', 'tab-owner')
  expect(websocketUrl).toHaveBeenCalledWith('tab-profile', 'tab-owner')
  await vi.advanceTimersByTimeAsync(60_000)
  expect(Socket.instances[0].readyState).not.toBe(3)
  Socket.instances[0].metadata()
  await starting
  expect(Socket.instances[0].sent[0]).toBe(META + '{"attached":"opaque-server-id"}')
  await api.closeSaved!('remote-pending')
  expect(Socket.instances).toHaveLength(2)
  expect(Socket.instances[1].url.searchParams.get('action')).toBe('close')
  expect(Socket.instances[1].url.searchParams.get('attach')).toBe('opaque-server-id')
  expect(websocketUrl).toHaveBeenLastCalledWith('tab-profile', 'tab-owner')
})

it('closes an unpublished relayed startup without waiting for metadata', async () => {
  const { createBrowserTerminal } = await import('./browser-terminal')
  Socket.reply = () => undefined
  const api = createBrowserTerminal({ basePath: '', currentProfile: () => null, currentConnection: () => 'saved-gateway',
    defaultCwd: async () => '/remote', websocketUrl: async () => 'ws://localhost/terminal' })
  const starting = api.start({ restoreKey: 'remote-pending' })
  const failed = expect(starting).rejects.toThrow('cancelled')
  await vi.waitFor(() => expect(Socket.instances).toHaveLength(1))
  expect(await api.closeSaved!('remote-pending')).toBe(true)
  await failed
  expect(Socket.instances).toHaveLength(1)
  expect(Socket.instances[0].readyState).toBe(3)
})

it('bounds a fresh executor handshake when the receiver never announces preparation', async () => {
  window.__HERMES_TERMINAL_ENVIRONMENT__ = 'executor'
  Socket.acceptStartupCancellation = false
  Socket.reply = () => undefined
  const api = install()
  const failed = expect(api.start({ restoreKey: 'executor-no-metadata' }))
    .rejects.toThrow('did not provide startup metadata')
  await vi.waitFor(() => expect(Socket.instances).toHaveLength(1))
  await vi.advanceTimersByTimeAsync(10_000)
  await failed
  expect(Socket.instances[0].readyState).toBe(3)
})

it('closes a fresh local executor terminal before the receiver sends any metadata', async () => {
  window.__HERMES_TERMINAL_ENVIRONMENT__ = 'executor'
  Socket.acceptStartupCancellation = false
  Socket.reply = () => undefined
  const api = install()
  const failed = expect(api.start({ restoreKey: 'executor-close-before-metadata' })).rejects.toThrow('cancelled')
  await vi.waitFor(() => expect(Socket.instances).toHaveLength(1))
  expect(await api.closeSaved!('executor-close-before-metadata')).toBe(true)
  await failed
  expect(Socket.instances).toHaveLength(1)
  expect(Socket.instances[0].readyState).toBe(3)
})

it('finishes closing a relay when startup fails before reaching the receiver', async () => {
  const { createBrowserTerminal } = await import('./browser-terminal')
  Socket.acceptStartupCancellation = false
  Socket.reply = () => undefined
  const api = createBrowserTerminal({ basePath: '', currentProfile: () => null, currentConnection: () => 'remote-gateway',
    defaultCwd: async () => '', websocketUrl: async () => 'ws://localhost/terminal' })
  const failed = expect(api.start({ restoreKey: 'relay-startup-failure' })).rejects.toThrow('Host terminal closed')
  await vi.waitFor(() => expect(Socket.instances).toHaveLength(1))
  const closing = api.closeSaved!('relay-startup-failure')
  Socket.instances[0].close(1006)
  expect(await closing).toBe(true)
  await failed
  expect(Socket.instances).toHaveLength(1)
})

it('waits for a legacy receiver token before disposing its startup socket', async () => {
  const { createBrowserTerminal } = await import('./browser-terminal')
  Socket.acceptStartupCancellation = false
  Socket.reply = socket => {
    if (socket.url.searchParams.get('action') === 'close') {
      socket.metadata({ terminalId: socket.url.searchParams.get('attach'), closed: true })
      socket.close()
    }
  }
  const api = createBrowserTerminal({ basePath: '', currentProfile: () => null, currentConnection: () => 'old-receiver',
    defaultCwd: async () => '', websocketUrl: async () => 'ws://localhost/terminal' })
  const starting = api.start({ restoreKey: 'old-start' })
  await vi.waitFor(() => expect(Socket.instances).toHaveLength(1))
  const disposing = api.closeSaved!('old-start')
  await vi.advanceTimersByTimeAsync(100)
  expect(Socket.instances[0].readyState).toBe(Socket.OPEN)
  Socket.instances[0].metadata()
  await starting
  expect(await disposing).toBe(true)
  expect(Socket.instances[1].url.searchParams.get('action')).toBe('close')
  expect(Socket.instances[1].url.searchParams.get('attach')).toBe('opaque-server-id')
})

it('pins the receiving environment after the token expires, including a fresh retry', async () => {
  const { createBrowserTerminal } = await import('./browser-terminal')
  const api = createBrowserTerminal({ basePath: '', currentProfile: () => null, currentConnection: () => 'saved-gateway',
    defaultCwd: async () => '', websocketUrl: async () => 'ws://localhost/terminal' })
  Socket.reply = socket => socket.metadata({ environment: 'executor' })
  await api.start({ restoreKey: 'pinned-receiver' })
  expect(Socket.instances[0].url.searchParams.has('environment')).toBe(false)

  Socket.instances[0].close(4410, 'expired')
  Socket.reply = socket => socket.close(4411, 'Terminal environment changed; reload the WebApp')
  await expect(api.start({ restoreKey: 'pinned-receiver' })).rejects.toThrow('environment changed')
  expect(Socket.instances[1].url.searchParams.get('environment')).toBe('executor')
  expect(Socket.instances[1].url.searchParams.get('action')).toBe('start')
  expect(Socket.instances[1].url.searchParams.has('attach')).toBe(false)
})

it('refuses a fresh direct retry against a receiver without placement support', async () => {
  window.__HERMES_TERMINAL_ENVIRONMENT__ = 'executor'
  const api = install()
  Socket.reply = socket => socket.metadata({ environment: 'executor' })
  await api.start({ restoreKey: 'direct-executor' })

  Socket.instances[0].close(4410, 'expired')
  Socket.acceptStartupCancellation = false
  const startedGatewayShell = vi.fn()
  Socket.reply = socket => {
    if (socket.url.searchParams.get('action') === 'start') {
      socket.close(4403, 'Terminal belongs to another identity or profile')
    } else {
      startedGatewayShell()
      socket.metadata()
    }
  }
  await expect(api.start({ restoreKey: 'direct-executor' })).rejects.toThrow('Terminal belongs to another identity or profile')
  expect(startedGatewayShell).not.toHaveBeenCalled()
})
