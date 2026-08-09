// 哈尔密斯（Hermes-live 微信小程序端）。
// 服务端协议与 web/ 完全一致：JSON 控制消息 + 1 字节前缀二进制音频帧
//   上行 0x01 + PCM16LE 16k（1024B/帧）；下行 0x01 + turn(u8) + PCM16LE 24k。
App({
  globalData: {
    server: wx.getStorageSync('hl_server') || 'wss://live.gaochengbin.com',
    token:  wx.getStorageSync('hl_token')  || 'eab040d215a50936e435fa32'
  }
});
