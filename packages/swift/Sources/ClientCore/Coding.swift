import Foundation

/// 契約 (snake_case、ISO 8601) と Swift (camelCase) の相互変換をまとめる。
public enum ContractCoding {
    public static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            if let date = parseISO8601(raw) { return date }
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "日時を解釈できません: \(raw)")
        }
        return decoder
    }

    public static func encoder(pretty: Bool = false) -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var container = encoder.singleValueContainer()
            try container.encode(formatISO8601(date))
        }
        if pretty {
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        } else {
            encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        }
        return encoder
    }

    private static func formatter(fractional: Bool) -> ISO8601DateFormatter {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = fractional ? [.withInternetDateTime, .withFractionalSeconds] : [.withInternetDateTime]
        return formatter
    }

    public static func parseISO8601(_ raw: String) -> Date? {
        formatter(fractional: true).date(from: raw) ?? formatter(fractional: false).date(from: raw)
    }

    public static func formatISO8601(_ date: Date) -> String {
        formatter(fractional: true).string(from: date)
    }
}
