import { useState, useRef, useCallback, useEffect } from "react";

// Web Speech API 类型声明(浏览器原生 API,不在标准 TS lib 中)
interface SpeechRecognitionAlternative {
  transcript: string;
  confidence: number;
}
interface SpeechRecognitionResult {
  readonly length: number;
  isFinal: boolean;
  item(index: number): SpeechRecognitionAlternative;
  [index: number]: SpeechRecognitionAlternative;
}
interface SpeechRecognitionResultList {
  readonly length: number;
  item(index: number): SpeechRecognitionResult;
  [index: number]: SpeechRecognitionResult;
}
interface SpeechRecognitionEvent extends Event {
  resultIndex: number;
  results: SpeechRecognitionResultList;
}
interface SpeechRecognitionErrorEvent extends Event {
  error: string;
  message: string;
}
interface SpeechRecognitionLike extends EventTarget {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((event: SpeechRecognitionEvent) => void) | null;
  onerror: ((event: SpeechRecognitionErrorEvent) => void) | null;
  onend: (() => void) | null;
}

type SRConstructor = new () => SpeechRecognitionLike;

function getSpeechRecognition(): SRConstructor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: SRConstructor;
    webkitSpeechRecognition?: SRConstructor;
  };
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

/**
 * 语音输入 hook——封装 Web Speech API (SpeechRecognition)。
 *
 * 浏览器支持:Chrome / Edge 全支持,Firefox 不支持(按钮自动隐藏)。
 * 对标 ChatGPT 语音输入 / Google Docs 语音打字。
 *
 * @param lang BCP-47 语言代码(如 "zh-CN" / "en-US"),从 i18n.language 映射
 * @param onResult 回调:识别到的文本 + 是否为最终结果
 */
export function useVoiceInput(
  lang: string,
  onResult: (text: string, isFinal: boolean) => void,
) {
  const [listening, setListening] = useState(false);
  const [supported, setSupported] = useState(false);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  // onResult 存入 ref——避免它变化时重建 recognition 实例
  const callbackRef = useRef(onResult);
  callbackRef.current = onResult;

  useEffect(() => {
    setSupported(!!getSpeechRecognition());
    return () => {
      recognitionRef.current?.abort();
      recognitionRef.current = null;
    };
  }, []);

  const start = useCallback(() => {
    const SR = getSpeechRecognition();
    if (!SR) return;
    // 避免重复启动
    if (recognitionRef.current) {
      recognitionRef.current.abort();
    }
    const recognition = new SR();
    recognition.lang = lang;
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.onresult = (event) => {
      let finalText = "";
      let interimText = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        const transcript = result[0]?.transcript || "";
        if (result.isFinal) finalText += transcript;
        else interimText += transcript;
      }
      if (finalText) {
        callbackRef.current(finalText, true);
      } else if (interimText) {
        callbackRef.current(interimText, false);
      }
    };
    recognition.onerror = (event) => {
      // no-speech(用户没说话)/ aborted(主动停止)不算错误,静默处理
      if (event.error !== "no-speech" && event.error !== "aborted") {
        console.warn("[voice-input]", event.error, event.message);
      }
      setListening(false);
    };
    recognition.onend = () => {
      setListening(false);
    };
    try {
      recognition.start();
      recognitionRef.current = recognition;
      setListening(true);
    } catch {
      // start() 在已运行时会抛 InvalidStateError——静默
      setListening(false);
    }
  }, [lang]);

  const stop = useCallback(() => {
    recognitionRef.current?.stop();
    setListening(false);
  }, []);

  const toggle = useCallback(() => {
    if (listening) stop();
    else start();
  }, [listening, start, stop]);

  return { listening, supported, start, stop, toggle };
}

/** 将 i18n 语言代码映射为 BCP-47(Web Speech API 用的格式) */
export function mapI18nToBCP47(i18nLang: string): string {
  const map: Record<string, string> = {
    zh: "zh-CN",
    en: "en-US",
    ja: "ja-JP",
    ko: "ko-KR",
  };
  const key = i18nLang.split("-")[0];
  return map[key] || "en-US";
}
