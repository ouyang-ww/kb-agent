# Python 异步编程

## async/await
asyncio 是 Python 的异步 I/O 框架。async def 定义协程，await 挂起等待。

## 关键概念
- 事件循环（event loop）：调度协程
- 协程（coroutine）：可挂起恢复的函数
- 任务（Task）：并发调度的协程

## 示例
import asyncio

async def main():
    print("开始")
    await asyncio.sleep(1)
    print("结束")

asyncio.run(main())

## 适用场景
网络请求、数据库访问、文件 I/O 等阻塞操作；CPU 密集任务不适合，应使用多进程。
