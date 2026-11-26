import { LOCAL_CONNECTION_ID } from '@hermes/shared'
import { computed } from 'nanostores'

import type { DesktopConnectionsRegistry, DesktopRegistryConnection } from '@/global'
import { isBrowserHostedDesktop } from '@/lib/platform'
import { $connectionsRegistry } from '@/store/connections'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { $connection } from '@/store/session'

import { bindTerminalDefaultGateway, browserTerminalGateway, forgetRemovedTerminalGateways, type TerminalEntry } from './terminals'

/** The gateway a terminal tab was opened on, and any profile selected for its shell. */
export interface TerminalGateway {
  connectionId: string
  profile?: string
}

/** Resolve an explicitly selected host. In Webapp, This device selects the
 *  profile serving the page, independent of the active remote connection. */
export function shellGatewayFor(
  connectionId: null | string | undefined,
  profile: null | string | undefined,
  registry: DesktopConnectionsRegistry | null | undefined
): TerminalGateway | undefined {
  const source = connectionId ? registry?.connections.find(connection => connection.id === connectionId) : undefined

  if (!source) {
    return undefined
  }

  if (isBrowserHostedDesktop() && source.kind === 'local') {
    return { connectionId: LOCAL_CONNECTION_ID, profile: 'current' }
  }

  return source.kind === 'ssh'
    ? { connectionId: source.id, profile: normalizeProfileKey(profile) }
    : { connectionId: LOCAL_CONNECTION_ID }
}

/** A new terminal opens on the gateway picked in Sessions. */
const $defaultTerminalGateway = computed(
  [$connection, $activeGatewayProfile, $connectionsRegistry],
  (connection, profile, registry) => browserTerminalGateway() ?? shellGatewayFor(connection?.connectionId, profile, registry)
)

export const defaultTerminalGateway = () => $defaultTerminalGateway.get()

/** A tab on a remote gateway can retry startup on that gateway. */
export const runsOnRemoteGateway = (gateway: TerminalGateway | undefined): boolean =>
  Boolean(gateway && gateway.connectionId !== LOCAL_CONNECTION_ID)

// New tabs record this gateway, and entering a session re-selects a tab in its
// cwd only on the session's host.
bindTerminalDefaultGateway($defaultTerminalGateway)

// A gateway that leaves the registry is gone for its tabs for good: ids are
// reusable label slugs, so a later gateway with the same name is another
// machine. An edit keeps the id registered and its tabs keep reconnecting.
$connectionsRegistry.subscribe(registry => {
  if (registry) {
    forgetRemovedTerminalGateways(new Set(registry.connections.map(connection => connection.id)))
  }
})

export const terminalGatewayOf = (term: TerminalEntry | undefined): TerminalGateway | undefined =>
  term?.connectionId ? { connectionId: term.connectionId, ...(term.profile ? { profile: term.profile } : {}) } : undefined

/** SSH gateways a terminal can be opened on, beside This device. */
export const $terminalShellGateways = computed($connectionsRegistry, registry =>
  (registry?.connections ?? []).filter(connection => connection.kind === 'ssh')
)

export const $localGateway = computed(
  $connectionsRegistry,
  registry => registry?.connections.find(connection => connection.id === LOCAL_CONNECTION_ID) ?? null
)

/** What a tab's label says about its host: the registry entry, `removed` when
 *  the gateway is gone, or null when the tab recorded none. */
export type TerminalHost = { connection: Pick<DesktopRegistryConnection, 'id' | 'kind' | 'label'> } | 'removed' | null

export function terminalHost(term: TerminalEntry, registry: DesktopConnectionsRegistry | null): TerminalHost {
  if (term.kind !== 'user' || !term.connectionId) {
    return null
  }

  if (term.gatewayRemoved) {
    return 'removed'
  }

  if (!registry) {
    return null
  }

  const connection = registry.connections.find(entry => entry.id === term.connectionId)

  return connection ? { connection } : 'removed'
}
