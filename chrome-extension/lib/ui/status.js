import { a as require_jsx_runtime, c as __toESM, i as TabItem, o as require_client, r as Button, s as require_react, t as AuthTokenSection } from "./authToken.js";
//#region src/ui/status.tsx
var import_react = /* @__PURE__ */ __toESM(require_react());
var import_client = require_client();
var import_jsx_runtime = require_jsx_runtime();
var StatusApp = () => {
	const [connections, setConnections] = (0, import_react.useState)([]);
	const loadStatus = async () => {
		const statuses = (await chrome.runtime.sendMessage({ type: "getConnectionStatus" })).connections ?? [];
		setConnections(await Promise.all(statuses.map(async ({ id, clientName, connectedTabIds }) => {
			return {
				id,
				clientName,
				tabs: (await Promise.all(connectedTabIds.map((tabId) => chrome.tabs.get(tabId).catch(() => void 0)))).filter((tab) => !!tab)
			};
		})));
	};
	(0, import_react.useEffect)(() => {
		loadStatus();
	}, []);
	const openTab = async (tabId) => {
		await chrome.tabs.update(tabId, { active: true });
		window.close();
	};
	const disconnect = async (connectionId) => {
		await chrome.runtime.sendMessage({
			type: "disconnect",
			connectionId
		});
		await loadStatus();
	};
	return /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", {
		className: "app-container",
		children: /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
			className: "content-wrapper",
			children: [connections.length > 0 ? connections.map((connection) => /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
				className: "connection",
				children: [
					/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
						className: "connection-header",
						children: [/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
							className: "client-info",
							children: ["Connected to ", /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("strong", { children: [
								"\"",
								connection.clientName || "unknown",
								"\""
							] })]
						}), /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Button, {
							variant: "primary",
							onClick: () => disconnect(connection.id),
							children: "Disconnect"
						})]
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", {
						className: "tab-section-title",
						children: connection.tabs.length === 1 ? "Accessible page:" : "Accessible pages:"
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", { children: connection.tabs.map((tab) => /* @__PURE__ */ (0, import_jsx_runtime.jsx)(TabItem, {
						tab,
						onClick: () => openTab(tab.id)
					}, tab.id)) })
				]
			}, connection.id)) : /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", {
				className: "status-banner",
				children: "No clients are currently connected. IO connects here while it works in Chrome. Copy the token below into IO's Settings > Browser."
			}), /* @__PURE__ */ (0, import_jsx_runtime.jsx)(AuthTokenSection, {})]
		})
	});
};
var container = document.getElementById("root");
if (container) (0, import_client.createRoot)(container).render(/* @__PURE__ */ (0, import_jsx_runtime.jsx)(StatusApp, {}));
//#endregion
