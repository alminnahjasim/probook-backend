import re

with open('d:/jasim/service-book-erp/templates/booking.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Javascript change for updatePaxRows()
content = content.replace("            if (s === 'tour') {", "            if (s !== 'rentacar' && s !== 'room_rent' && s !== 'typing') {")

idx_left_end = content.find("                                        </td>\n                                    </tr>\n                                </table>\n                            </td>\n                            <!-- RIGHT COLUMN -->")
if idx_left_end == -1:
    print("Could not find left end")

idx_right_end_table = content.find("                                </table>\n                            </td>\n                        </tr>\n                        <!-- BOTTOM Terms and Signature -->")

# Let's extract the left bottom content
idx_left_bottom_start = content.find("                        <!-- BOTTOM Terms and Signature -->\n                        <tr>\n                            <td style=\"padding: 10px; border-right: 2px solid #000; font-size: 9px; vertical-align: top;\">\n")
idx_left_bottom_start += len("                        <!-- BOTTOM Terms and Signature -->\n                        <tr>\n                            <td style=\"padding: 10px; border-right: 2px solid #000; font-size: 9px; vertical-align: top;\">\n")

idx_left_bottom_end = content.find("                            </td>\n                            <td style=\"padding: 10px; vertical-align: top;\">", idx_left_bottom_start)

left_bottom_content = content[idx_left_bottom_start:idx_left_bottom_end]

idx_right_bottom_start = idx_left_bottom_end + len("                            </td>\n                            <td style=\"padding: 10px; vertical-align: top;\">\n")
idx_right_bottom_end = content.find("                            </td>\n                        </tr>\n                    </table>\n                </div>`;", idx_right_bottom_start)

right_bottom_content = content[idx_right_bottom_start:idx_right_bottom_end]

# Now reconstruct!
if idx_left_end > 0 and idx_right_end_table > 0:
    new_left_bottom = '<div style="padding: 10px; font-size: 9px;">\n' + left_bottom_content + '                            </div>\n'
    new_right_bottom = '<div style="padding: 10px;">\n' + right_bottom_content + '                            </div>\n'

    # We substitute
    left_table_end_str = "                                </table>\n                            </td>\n                            <!-- RIGHT COLUMN -->"
    left_table_new_str = "                                </table>\n" + new_left_bottom + "                            </td>\n                            <!-- RIGHT COLUMN -->"
    content = content.replace(left_table_end_str, left_table_new_str)
    
    right_table_end_str = "                                </table>\n                            </td>\n                        </tr>\n                        <!-- BOTTOM Terms and Signature -->"
    right_table_new_str = "                                </table>\n" + new_right_bottom + "                            </td>\n                        </tr>"
    content = content.replace(right_table_end_str, right_table_new_str)
    
    block_to_remove_start = content.find("                        <!-- BOTTOM Terms and Signature -->")
    block_to_remove_end = content.find("                    </table>\n                </div>`;", block_to_remove_start)
    
    if block_to_remove_start > 0 and block_to_remove_end > 0:
        content = content[:block_to_remove_start] + "                    </table>\n                </div>`;" + content[block_to_remove_end + len("                    </table>\n                </div>`;"):]
        
    with open('d:/jasim/service-book-erp/templates/booking.html', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Patch applied successfully.")
else:
    print("Failed to find indices.")
