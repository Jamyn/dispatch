// happy-dom implements neither window.visualViewport nor ResizeObserver;
// Vuetify's overlay positioning code requires both, so every test that opens
// a menu or dialog crashes without these stubs.
class VisualViewportStub extends EventTarget {
  constructor() {
    super()
    this.width = 1920
    this.height = 1080
    this.offsetLeft = 0
    this.offsetTop = 0
    this.pageLeft = 0
    this.pageTop = 0
    this.scale = 1
  }
}

if (!globalThis.visualViewport) {
  globalThis.visualViewport = new VisualViewportStub()
}

if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}
