//#region src/relayConnection.ts
/**
* Copyright (c) Microsoft Corporation.
*
* Licensed under the Apache License, Version 2.0 (the "License");
* you may not use this file except in compliance with the License.
* You may obtain a copy of the License at
*
* http://www.apache.org/licenses/LICENSE-2.0
*
* Unless required by applicable law or agreed to in writing, software
* distributed under the License is distributed on an "AS IS" BASIS,
* WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
* See the License for the specific language governing permissions and
* limitations under the License.
*/
function debugLog(...args) {
	console.log("[Extension]", ...args);
}
var ALLOWED_CHROME_COMMANDS = /* @__PURE__ */ new Set([
	"chrome.debugger.attach",
	"chrome.debugger.detach",
	"chrome.debugger.sendCommand",
	"chrome.tabs.create",
	"chrome.tabs.remove"
]);
var CHROME_EVENT_METHODS = [
	"chrome.debugger.onEvent",
	"chrome.debugger.onDetach",
	"chrome.tabs.onCreated",
	"chrome.tabs.onRemoved"
];
var REATTACH_DELAY_MS = 150;
var REATTACH_VERIFY_MS = 2500;
var REATTACH_COOLDOWN_MS = 3e3;
var RelayConnection = class {
	_ws;
	_attachedTabs = /* @__PURE__ */ new Set();
	_hasEverAttached = false;
	_eventListeners = [];
	_closed = false;
	_pendingReattach = /* @__PURE__ */ new Set();
	_recentReattach = /* @__PURE__ */ new Set();
	onclose;
	ontabattached;
	ontabdetached;
	get attachedTabs() {
		return this._attachedTabs;
	}
	constructor(ws) {
		this._ws = ws;
		this._installEventForwarders();
		this._ws.onmessage = this._onMessage.bind(this);
		this._ws.onclose = () => this._onClose();
	}
	didInitialize() {
		this._sendMessage({
			method: "extension.initialized",
			params: []
		});
	}
	close(message) {
		this._ws.close(1e3, message);
		this._onClose();
	}
	attachTab(tab) {
		if (this._closed || this._attachedTabs.has(tab.id)) return;
		this._sendMessage({
			method: "chrome.tabs.onCreated",
			params: [tab]
		});
	}
	detachTab(tabId) {
		if (this._closed || !this._attachedTabs.has(tabId)) return;
		chrome.debugger.detach({ tabId }).catch((error) => {
			debugLog("Error detaching tab:", error);
		});
		this._notifyTabDetached(tabId);
		this._sendMessage({
			method: "chrome.debugger.onDetach",
			params: [{ tabId }, "target_closed"]
		});
		this._checkLastTabDetached();
	}
	_notifyTabAttached(tabId) {
		this._attachedTabs.add(tabId);
		this._hasEverAttached = true;
		this._pendingReattach.delete(tabId);
		this.ontabattached?.(tabId);
	}
	_notifyTabDetached(tabId) {
		this._attachedTabs.delete(tabId);
		this.ontabdetached?.(tabId);
	}
	_installEventForwarders() {
		for (const fullMethod of CHROME_EVENT_METHODS) {
			const target = resolveChromeMember(fullMethod);
			const listener = (...args) => this._onChromeEvent(fullMethod, args);
			target.obj[target.name].addListener(listener);
			this._eventListeners.push({ remove: () => target.obj[target.name].removeListener(listener) });
		}
	}
	_onClose() {
		if (this._closed) return;
		this._closed = true;
		this._pendingReattach.clear();
		this._recentReattach.clear();
		for (const l of this._eventListeners) l.remove();
		this._eventListeners = [];
		for (const tabId of [...this._attachedTabs]) {
			chrome.debugger.detach({ tabId }).catch(() => {});
			this._notifyTabDetached(tabId);
		}
		this.onclose?.();
	}
	_checkLastTabDetached() {
		if (this._hasEverAttached && this._attachedTabs.size === 0 && this._pendingReattach.size === 0) this.close("All controlled tabs detached");
	}
	_onChromeEvent(fullMethod, args) {
		const tabId = this._tabIdForEventArgs(fullMethod, args);
		if (tabId === void 0 || !this._attachedTabs.has(tabId)) return;
		this._sendMessage({
			method: fullMethod,
			params: args
		});
		if (fullMethod === "chrome.debugger.onDetach") {
			const reason = args[1];
			this._notifyTabDetached(tabId);
			if (reason === "target_closed" && this._maybeScheduleReattach(tabId)) return;
			this._checkLastTabDetached();
		}
	}
	_maybeScheduleReattach(tabId) {
		if (this._closed) return false;
		if (this._recentReattach.has(tabId)) {
			debugLog(`Not re-attaching tab ${tabId}: re-detached within ${REATTACH_COOLDOWN_MS}ms`);
			return false;
		}
		this._recentReattach.add(tabId);
		setTimeout(() => this._recentReattach.delete(tabId), REATTACH_COOLDOWN_MS);
		this._pendingReattach.add(tabId);
		setTimeout(() => void this._tryReattach(tabId), REATTACH_DELAY_MS);
		return true;
	}
	_reattachAborted(tabId) {
		return this._closed || !this._pendingReattach.has(tabId);
	}
	async _tryReattach(tabId) {
		if (this._reattachAborted(tabId)) return;
		let tab;
		try {
			tab = await chrome.tabs.get(tabId);
		} catch {
			this._pendingReattach.delete(tabId);
			this._checkLastTabDetached();
			return;
		}
		if (this._reattachAborted(tabId)) return;
		if (this._attachedTabs.has(tabId)) {
			this._pendingReattach.delete(tabId);
			return;
		}
		this.attachTab(tab);
		setTimeout(() => {
			if (this._reattachAborted(tabId)) return;
			this._pendingReattach.delete(tabId);
			if (!this._attachedTabs.has(tabId)) this._checkLastTabDetached();
		}, REATTACH_VERIFY_MS);
	}
	_tabIdForEventArgs(fullMethod, args) {
		switch (fullMethod) {
			case "chrome.debugger.onEvent":
			case "chrome.debugger.onDetach": return args[0]?.tabId;
			case "chrome.tabs.onCreated": return args[0].openerTabId;
			case "chrome.tabs.onRemoved": return args[0];
		}
	}
	_onMessage(event) {
		this._onMessageAsync(event).catch((e) => debugLog("Error handling message:", e));
	}
	async _onMessageAsync(event) {
		let message;
		try {
			message = JSON.parse(event.data);
		} catch (error) {
			debugLog(`Error parsing message ${event.data}:`, error);
			this._sendError(-32700, `Error parsing message: ${error.message}`);
			return;
		}
		const response = { id: message.id };
		try {
			response.result = await this._handleCommand(message);
		} catch (error) {
			debugLog(`Error handling command ${JSON.stringify(message)}:`, error);
			response.error = error.message;
		}
		this._sendMessage(response);
	}
	async _handleCommand(message) {
		if (!ALLOWED_CHROME_COMMANDS.has(message.method)) throw new Error(`Unknown method: ${message.method}`);
		const args = message.params ?? [];
		const result = await invokeChromeMethod(message.method, args);
		if (message.method === "chrome.debugger.attach") {
			const target = args[0];
			if (target?.tabId !== void 0) this._notifyTabAttached(target.tabId);
		}
		return result ?? {};
	}
	_sendError(code, message) {
		this._sendMessage({ error: {
			code,
			message
		} });
	}
	_sendMessage(message) {
		if (this._ws.readyState === WebSocket.OPEN) this._ws.send(JSON.stringify(message));
	}
};
function resolveChromeMember(fullMethod) {
	const parts = fullMethod.split(".");
	if (parts[0] !== "chrome" || parts.length < 3) throw new Error(`Invalid chrome method: ${fullMethod}`);
	let obj = chrome;
	for (let i = 1; i < parts.length - 1; i++) {
		obj = obj?.[parts[i]];
		if (obj === void 0) throw new Error(`Unknown chrome path: ${parts.slice(0, i + 1).join(".")}, calling ${fullMethod}`);
	}
	return {
		obj,
		name: parts[parts.length - 1]
	};
}
async function invokeChromeMethod(fullMethod, args) {
	const { obj, name } = resolveChromeMember(fullMethod);
	const fn = obj[name];
	if (typeof fn !== "function") throw new Error(`Not a function: ${fullMethod}`);
	return await fn.apply(obj, args);
}
//#endregion
//#region src/pendingConnection.ts
/**
* Copyright (c) Microsoft Corporation.
*
* Licensed under the Apache License, Version 2.0 (the "License");
* you may not use this file except in compliance with the License.
* You may obtain a copy of the License at
*
* http://www.apache.org/licenses/LICENSE-2.0
*
* Unless required by applicable law or agreed to in writing, software
* distributed under the License is distributed on an "AS IS" BASIS,
* WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
* See the License for the specific language governing permissions and
* limitations under the License.
*/
var PendingConnections = class {
	_map = /* @__PURE__ */ new Map();
	constructor() {
		chrome.tabs.onRemoved.addListener((tabId) => this._map.delete(tabId));
	}
	create(selectorTabId, mcpRelayUrl) {
		this._map.set(selectorTabId, mcpRelayUrl);
	}
	has(selectorTabId) {
		return this._map.has(selectorTabId);
	}
	async take(selectorTabId) {
		const mcpRelayUrl = this._map.get(selectorTabId);
		if (mcpRelayUrl === void 0) return void 0;
		this._map.delete(selectorTabId);
		return openRelayConnection(mcpRelayUrl);
	}
};
async function openRelayConnection(mcpRelayUrl) {
	try {
		const socket = new WebSocket(mcpRelayUrl);
		await new Promise((resolve, reject) => {
			socket.onopen = () => resolve();
			socket.onerror = () => reject(/* @__PURE__ */ new Error("WebSocket error"));
			setTimeout(() => reject(/* @__PURE__ */ new Error("Connection timeout")), 5e3);
		});
		return new RelayConnection(socket);
	} catch (error) {
		const message = `Failed to connect to MCP relay: ${error.message}`;
		debugLog(message);
		throw new Error(message);
	}
}
//#endregion
//#region src/connectedTabGroup.ts
/**
* Copyright (c) Microsoft Corporation.
*
* Licensed under the Apache License, Version 2.0 (the "License");
* you may not use this file except in compliance with the License.
* You may obtain a copy of the License at
*
* http://www.apache.org/licenses/LICENSE-2.0
*
* Unless required by applicable law or agreed to in writing, software
* distributed under the License is distributed on an "AS IS" BASIS,
* WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
* See the License for the specific language governing permissions and
* limitations under the License.
*/
var PLAYWRIGHT_GROUP_TITLE = "IO";
var PLAYWRIGHT_GROUP_TITLE_PREFIX = `${PLAYWRIGHT_GROUP_TITLE} · `;
var PLAYWRIGHT_GROUP_COLORS = [
	"purple",
	"blue",
	"green",
	"orange",
	"pink",
	"cyan",
	"yellow",
	"red"
];
var NON_DEBUGGABLE_SCHEMES = [
	"chrome:",
	"edge:",
	"devtools:"
];
var CONNECTED_BADGE = {
	text: "✓",
	color: "#4CAF50",
	title: "IO is using this tab"
};
function isNonDebuggableUrl(url) {
	return !!url && NON_DEBUGGABLE_SCHEMES.some((s) => url.startsWith(s));
}
function uniqueGroupStyle(clientName, taken) {
	const titles = new Set(taken.map((style) => style.title));
	const base = !clientName || clientName === PLAYWRIGHT_GROUP_TITLE ? PLAYWRIGHT_GROUP_TITLE : PLAYWRIGHT_GROUP_TITLE_PREFIX + clientName;
	let title = base;
	for (let i = 2; titles.has(title); i++) title = `${base} (${i})`;
	const colors = new Set(taken.map((style) => style.color));
	const color = PLAYWRIGHT_GROUP_COLORS.find((candidate) => !colors.has(candidate)) ?? PLAYWRIGHT_GROUP_COLORS[0];
	return {
		title,
		color
	};
}
async function cleanupStalePlaywrightGroups() {
	try {
		const stale = (await chrome.tabGroups.query({})).filter((g) => g.title === PLAYWRIGHT_GROUP_TITLE || g.title?.startsWith(PLAYWRIGHT_GROUP_TITLE_PREFIX));
		const tabIds = (await Promise.all(stale.map((g) => chrome.tabs.query({ groupId: g.id })))).flat().map((t) => t.id).filter((id) => id !== void 0);
		if (tabIds.length) await ungroupTabs(tabIds);
	} catch (error) {
		debugLog("Error cleaning up stale groups:", error);
	}
}
var ConnectedTabGroup = class {
	clientName;
	groupStyle;
	_connection;
	_isTabReserved;
	_groupId = null;
	_groupTabIds = /* @__PURE__ */ new Set();
	_onTabUpdatedListener;
	_onTabRemovedListener;
	onclose;
	constructor(connection, selectedTab, clientName, groupStyle, isTabReserved) {
		this.clientName = clientName;
		this.groupStyle = groupStyle;
		this._isTabReserved = isTabReserved;
		this._connection = connection;
		this._connection.onclose = () => this._onConnectionClose();
		this._connection.ontabattached = (tabId) => this._onTabAttached(tabId);
		this._connection.ontabdetached = (tabId) => this._onTabDetached(tabId);
		this._onTabUpdatedListener = this._onTabUpdated.bind(this);
		this._onTabRemovedListener = this._onTabRemoved.bind(this);
		chrome.tabs.onUpdated.addListener(this._onTabUpdatedListener);
		chrome.tabs.onRemoved.addListener(this._onTabRemovedListener);
		this._connection.attachTab(selectedTab);
		this._connection.didInitialize();
	}
	connectedTabIds() {
		return [...this._groupTabIds];
	}
	close(reason) {
		this._connection.close(reason);
	}
	releaseTab(tabId) {
		if (!this._groupTabIds.has(tabId)) return;
		this._groupTabIds.delete(tabId);
		this._connection.detachTab(tabId);
	}
	_onTabUpdated(tabId, changeInfo, tab) {
		if (changeInfo.groupId !== void 0) this._onTabGroupChanged(tabId, tab);
		if (changeInfo.url === void 0) return;
		if (this._connection.attachedTabs.has(tabId)) this._updateBadge(tabId, CONNECTED_BADGE);
		else if (this._groupTabIds.has(tabId) && !isNonDebuggableUrl(changeInfo.url)) this._connection.attachTab(tab);
	}
	_onTabGroupChanged(tabId, tab) {
		const inOurGroup = this._groupId !== null && tab.groupId === this._groupId;
		if (inOurGroup === this._groupTabIds.has(tabId)) return;
		if (inOurGroup) {
			if (this._isTabReserved(tabId)) {
				ungroupTabs([tabId]);
				return;
			}
			this._groupTabIds.add(tabId);
			if (!isNonDebuggableUrl(tab.url)) this._connection.attachTab(tab);
		} else {
			this._groupTabIds.delete(tabId);
			if (this._connection.attachedTabs.has(tabId)) this._connection.detachTab(tabId);
		}
	}
	_onTabRemoved(tabId) {
		this._groupTabIds.delete(tabId);
	}
	_onTabAttached(tabId) {
		this._updateBadge(tabId, CONNECTED_BADGE);
		this._addTabToGroup(tabId);
	}
	_onTabDetached(tabId) {
		this._updateBadge(tabId, { text: "" });
	}
	_onConnectionClose() {
		chrome.tabs.onUpdated.removeListener(this._onTabUpdatedListener);
		chrome.tabs.onRemoved.removeListener(this._onTabRemovedListener);
		const groupTabs = [...this._groupTabIds];
		this._groupTabIds.clear();
		if (groupTabs.length) closeGroupTabs(groupTabs);
		this.onclose?.();
	}
	async _updateBadge(tabId, { text, color, title }) {
		try {
			await Promise.all([
				chrome.action.setBadgeText({
					tabId,
					text
				}),
				chrome.action.setTitle({
					tabId,
					title: title || ""
				}),
				color ? chrome.action.setBadgeBackgroundColor({
					tabId,
					color
				}) : Promise.resolve()
			]);
		} catch (error) {}
	}
	async _addTabToGroup(tabId) {
		if (this._groupTabIds.has(tabId)) return;
		try {
			await retryOnDrag(async () => {
				if (this._groupId === null) {
					this._groupId = await chrome.tabs.group({ tabIds: [tabId] });
					await chrome.tabGroups.update(this._groupId, this.groupStyle);
				} else await chrome.tabs.group({
					groupId: this._groupId,
					tabIds: [tabId]
				});
			});
			this._groupTabIds.add(tabId);
		} catch (error) {
			debugLog("Error adding tab to group:", error);
		}
	}
};
// IO: when the agent disconnects, its tabs go with it instead of being left behind ungrouped. A tab that is the last
// one in its window is only ungrouped, so closing the group never closes a Chrome window.
async function closeGroupTabs(tabIds) {
	try {
		const tabs = (await Promise.all(tabIds.map((id) => chrome.tabs.get(id).catch(() => null)))).filter(Boolean);
		const close = [];
		for (const tab of tabs) {
			const inWindow = await chrome.tabs.query({ windowId: tab.windowId });
			const closing = close.filter((t) => t.windowId === tab.windowId).length;
			if (inWindow.length - closing > 1) close.push(tab);
		}
		const keep = tabs.filter((tab) => !close.includes(tab)).map((tab) => tab.id);
		if (close.length) await retryOnDrag(() => chrome.tabs.remove(close.map((tab) => tab.id)));
		if (keep.length) await ungroupTabs(keep);
	} catch (error) {
		debugLog("Error closing group tabs:", error);
		await ungroupTabs(tabIds);
	}
}
async function ungroupTabs(tabIds) {
	try {
		await retryOnDrag(() => chrome.tabs.ungroup(tabIds));
	} catch (error) {
		debugLog("Error ungrouping tabs:", error);
	}
}
async function retryOnDrag(fn) {
	const delays = [
		0,
		100,
		200,
		400,
		800
	];
	let lastError;
	for (const delay of delays) {
		if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
		try {
			await fn();
			return;
		} catch (error) {
			if (!error?.message?.includes("user may be dragging a tab")) throw error;
			lastError = error;
		}
	}
	throw lastError;
}
//#endregion
//#region src/background.ts
/**
* Copyright (c) Microsoft Corporation.
*
* Licensed under the Apache License, Version 2.0 (the "License");
* you may not use this file except in compliance with the License.
* You may obtain a copy of the License at
*
* http://www.apache.org/licenses/LICENSE-2.0
*
* Unless required by applicable law or agreed to in writing, software
* distributed under the License is distributed on an "AS IS" BASIS,
* WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
* See the License for the specific language governing permissions and
* limitations under the License.
*/
var PlaywrightExtension = class {
	_connections = /* @__PURE__ */ new Map();
	_lastConnectionId = 0;
	_pendingConnections = new PendingConnections();
	_cleanupPromise;
	constructor() {
		chrome.runtime.onMessage.addListener(this._onMessage.bind(this));
		chrome.action.onClicked.addListener(this._onActionClicked.bind(this));
		this._cleanupPromise = cleanupStalePlaywrightGroups();
	}
	_onMessage(message, sender, sendResponse) {
		switch (message.type) {
			case "connectionRequested": {
				const selectorTabId = sender.tab.id;
				this._releaseConnectPage(selectorTabId).then(() => {
					this._pendingConnections.create(selectorTabId, message.mcpRelayUrl);
					sendResponse({ success: true });
				});
				return true;
			}
			case "getTabs":
				this._getTabs(sender.tab?.id).then((tabs) => sendResponse({
					success: true,
					tabs,
					currentTabId: sender.tab?.id
				}), (error) => sendResponse({
					success: false,
					error: error.message
				}));
				return true;
			case "connectToTab": {
				const selectedTab = message.tab ?? sender.tab;
				this._connectTab(sender.tab.id, selectedTab, message.clientName).then(() => sendResponse({ success: true }), (error) => sendResponse({
					success: false,
					error: error.message
				}));
				return true;
			}
			case "getConnectionStatus":
				sendResponse({ connections: [...this._connections].map(([id, group]) => ({
					id,
					clientName: group.clientName,
					connectedTabIds: group.connectedTabIds()
				})) });
				return false;
			case "disconnect":
				this._connections.get(message.connectionId)?.close("User disconnected");
				sendResponse({ success: true });
				return false;
			case "keepalive": return false;
		}
	}
	async _connectTab(selectorTabId, tab, clientName) {
		try {
			await this._cleanupPromise;
			this._releaseTab(selectorTabId);
			if (tab.id !== selectorTabId && this._connectedTabIds().has(tab.id)) throw new Error("This tab is already connected to another client");
			const connection = await this._pendingConnections.take(selectorTabId);
			if (!connection) throw new Error("Pending client connection closed");
			const id = ++this._lastConnectionId;
			const group = new ConnectedTabGroup(connection, tab, clientName, uniqueGroupStyle(clientName, [...this._connections.values()].map((group) => group.groupStyle)), (tabId) => this._pendingConnections.has(tabId));
			group.onclose = () => this._connections.delete(id);
			this._connections.set(id, group);
			await Promise.all([chrome.tabs.update(tab.id, { active: true }), chrome.windows.update(tab.windowId, { focused: true })]).catch(() => {});
			if (tab.id !== selectorTabId) await chrome.tabs.remove(selectorTabId).catch(() => {});
		} catch (error) {
			debugLog(`Failed to connect tab ${tab.id}:`, error.message);
			throw error;
		}
	}
	async _releaseConnectPage(tabId) {
		this._releaseTab(tabId);
		await ungroupTabs([tabId]);
	}
	_releaseTab(tabId) {
		for (const group of this._connections.values()) group.releaseTab(tabId);
	}
	async _getTabs(selectorTabId) {
		const tabs = await chrome.tabs.query({});
		const connectedTabIds = this._connectedTabIds();
		return tabs.filter((tab) => !isNonDebuggableUrl(tab.url) && (tab.id === selectorTabId || !connectedTabIds.has(tab.id)));
	}
	_connectedTabIds() {
		return new Set([...this._connections.values()].flatMap((group) => group.connectedTabIds()));
	}
	async _onActionClicked() {
		await chrome.tabs.create({
			url: chrome.runtime.getURL("status.html"),
			active: true
		});
	}
};
new PlaywrightExtension();
//#endregion
