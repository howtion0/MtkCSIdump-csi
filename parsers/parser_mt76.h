#pragma once

#include "parser.h"

class ParserMT76 final : public Parser
{
public:
    std::vector<CsiPacket> processRawData(void *data) override;
};
