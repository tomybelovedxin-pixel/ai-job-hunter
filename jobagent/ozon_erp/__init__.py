"""Ozon 专用 ERP 原型（自建，可跑、可自测）。

解决的是俄罗斯 Ozon 卖家的日常：店铺商品与库存同步、FBS/FBO 订单同步、
财务流水对账、漏单检测、出日报表。

设计边界（写在这里免得以后自己骗自己）：
- 接口路径与字段是按 Ozon Seller API 的公开文档结构整理的，2026-10-15 在本机
  **没连上** docs.ozon.ru / api-seller.ozon.ru（代理对这两个域名 TLS 直接断），
  所以端点表集中放在 `client.ENDPOINTS`，接真实账号前先跑 `doctor` 校验一遍；
- 网络层可替换（`transport`），自带一个本地模拟卖家（`mock.MockOzon`），
  所以同步、幂等、重试、漏单检测、对账这些逻辑全部可以离线自测；
- 不绕验证码、不注入他人凭据；只用卖家自己的 Client-Id + Api-Key。
"""

__version__ = "0.1.0"
