// Imported into the generated service worker: shows alert notifications.
self.addEventListener("push", (event) => {
  let msg = { title: "P1 Monitor", body: "" };
  try { msg = event.data.json(); } catch (e) { msg.body = event.data ? event.data.text() : ""; }
  event.waitUntil(self.registration.showNotification(msg.title, {
    body: msg.body, icon: "/icon.svg", badge: "/icon.svg", data: msg.data || {}, tag: "p1-alert",
  }));
});
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(clients.matchAll({ type: "window" }).then((list) => {
    for (const c of list) { if ("focus" in c) { c.navigate("/#/alerts"); return c.focus(); } }
    return clients.openWindow("/#/alerts");
  }));
});
